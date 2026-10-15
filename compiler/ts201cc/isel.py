"""Register-class assignment and instruction selection (SSA IR -> machine IR).

Register classes:
  X  compute block X registers (ALU, multiplier, shifter)
  J  J-IALU registers (addresses, integer add/sub/logic, compares)
  K  K-IALU registers (same abilities as J; a second IALU and memory port)

Each packet can issue one J-IALU and one K-IALU operation (including memory
accesses, which use the base register's IALU), so address arithmetic is split
between J and K to balance the two units.
"""
from __future__ import annotations
from collections import defaultdict
from . import ir
from .ir import Ins, MASK, s32

REG_CLASSES = ('X', 'Y', 'J', 'K')
FLEX = {'add', 'sub', 'and', 'or', 'xor', 'andn', 'min', 'max', 'neg', 'not', 'abs'}
XONLY = {'mul', 'shl', 'shr', 'sar', 'mulhs', 'mulhu', 'mulq16', 'mulfr', 'clz', 'fext', 'clip'}
DIVS = {'div', 'udiv', 'mod', 'umod'}
IALU_OPS = {'add': '+', 'sub': '-', 'and': 'AND', 'or': 'OR', 'xor': 'XOR', 'andn': 'AND NOT'}


# ---------------------------------------------------------------------------
# min-cut (Dinic) for the two-way IALU/X partition

class _Flow:
    def __init__(self, n):
        self.n = n; self.g = [[] for _ in range(n)]

    def edge(self, a, b, c, rc=0.0):
        self.g[a].append([b, c, len(self.g[b])])
        self.g[b].append([a, rc, len(self.g[a])-1])

    def maxflow(self, s, t):
        total = 0.0
        g = self.g
        while True:
            level = [-1]*self.n; level[s] = 0
            q = [s]
            for u in q:
                for v, c, _ in g[u]:
                    if c > 1e-12 and level[v] < 0:
                        level[v] = level[u]+1; q.append(v)
            if level[t] < 0: break
            it = [0]*self.n

            def dfs(u, f):
                if u == t: return f
                while it[u] < len(g[u]):
                    e = g[u][it[u]]
                    v, c, r = e
                    if c > 1e-12 and level[v] == level[u]+1:
                        d = dfs(v, min(f, c))
                        if d > 1e-12:
                            e[1] -= d; g[v][r][1] += d
                            return d
                    it[u] += 1
                return 0.0
            import sys
            old = sys.getrecursionlimit()
            if old < 10000: sys.setrecursionlimit(10000)
            while True:
                f = dfs(s, float('inf'))
                if f <= 1e-12: break
                total += f
        return total

    def source_side(self, s):
        seen = {s}; q = [s]
        for u in q:
            for v, c, _ in self.g[u]:
                if c > 1e-12 and v not in seen:
                    seen.add(v); q.append(v)
        return seen


def _is_const(defs, v):
    i = defs.get(v)
    return i is not None and i.op in ('const', 'sym')


def assign_classes(fn, defs):
    """Return {vreg: 'I'|'X'} for non-constant values (I = IALU, split later)."""
    nodes = set()
    for b in fn.blocks:
        for p in b.phis: nodes.add(p.d)
        for i in b.ins:
            if i.d is not None and i.op not in ('const', 'sym'): nodes.add(i.d)
    cI = defaultdict(float); cX = defaultdict(float)
    pairs = defaultdict(float)

    def pair(a, b, w):
        if a in nodes and b in nodes and a != b:
            pairs[(a, b) if a < b else (b, a)] += w

    def want(v, cls, w):
        if v not in nodes: return
        if cls == 'I': cX[v] += w
        else: cI[v] += w

    # Adds that only form one load/store address fold into [Jx + Jy] addressing.
    users = defaultdict(list)
    for b in fn.blocks:
        for i in b.ins:
            for x in i.a: users[x].append(i)
        for p in b.phis:
            for x in p.a: users[x].append(p)
    address_adds = set()
    for b in fn.blocks:
        for i in b.ins:
            if i.op == 'add' and not any(_is_const(defs, x) for x in i.a):
                us = users.get(i.d, [])
                if len(us) == 1 and us[0].op in ('load', 'store') and us[0].blk is b and us[0].a[0] == i.d                         and us[0].imm == 0 and (us[0].op == 'load' or us[0].a[1] != i.d):
                    address_adds.add(id(i))
    for b in fn.blocks:
        f = b.freq
        for p in b.phis:
            for pred, x in zip(p.t, p.a):
                pair(p.d, x, min(f, pred.freq))
        for i in b.ins:
            op = i.op; a = i.a
            if id(i) in address_adds:
                for x in a: want(x, 'I', f)
                want(i.d, 'I', 2*f)
                continue
            if op == 'loadq':
                want(a[0], 'I', f); want(i.d, 'X', 100*f)
                continue
            if op == 'storeq':
                want(a[0], 'I', f); want(a[1], 'X', 100*f)
                continue
            if op == 'load':
                want(a[0], 'I', f)
                if i.d in nodes: cI[i.d] += 0.02*f   # J/K data ports are single-ported
            elif op == 'store':
                want(a[0], 'I', f)
                if a[1] in nodes: cI[a[1]] += 0.02*f
            elif op in FLEX:
                for x in a:
                    if _is_const(defs, x):
                        cX[i.d] += 0.1*f   # X needs the constant in a register
                    else:
                        pair(i.d, x, f)
                if i.d in nodes: cI[i.d] += 0.2*f   # IALU slots are the scarce resource
            elif op in XONLY:
                want(i.d, 'X', f)
                for k, x in enumerate(a):
                    if op in ('shl', 'shr', 'sar') and k == 1 and _is_const(defs, x): continue
                    want(x, 'X', f)
            elif op in DIVS:
                for x in a: want(x, 'X', f)
                if op in ('udiv', 'div'): want(i.d, 'I', 0.3*f)
                else: want(i.d, 'X', f)
            elif op == 'sel':
                x, y, v1, v2 = a
                if _is_const(defs, y):
                    if x in nodes: cX[x] += 0.3*f
                else:
                    pair(x, y, f)
                for v in (v1, v2):
                    if not _is_const(defs, v): pair(i.d, v, f)
            elif op == 'cmp' or op == 'br':
                x, y = a
                if _is_const(defs, y):
                    if x in nodes: cX[x] += 0.3*f
                elif _is_const(defs, x):
                    if y in nodes: cX[y] += 0.3*f
                else:
                    pair(x, y, f)
                if op == 'cmp' and i.d in nodes: cI[i.d] += 0.01*f
            elif op == 'copy':
                pair(i.d, a[0], f)
            elif op == 'frame':
                want(i.d, 'I', f)
            elif op == 'call':
                if i.s is None: want(a[0], 'I', f)
                if i.d in nodes: cI[i.d] += 0.3*f
                for x in a[1 if i.s is None else 0:]:
                    if x in nodes: cI[x] += 0.02*f
            elif op == 'ret':
                if a and a[0] in nodes: cI[a[0]] += 0.3*f
            elif op == 'jtab':
                want(a[0], 'I', f)
    for v in nodes: cI[v] += 0.001   # tie-break: compute registers
    order = sorted(nodes)
    index = {v: k+2 for k, v in enumerate(order)}
    flow = _Flow(len(order)+2)
    S, T = 0, 1
    for v in order:
        if cX[v]: flow.edge(S, index[v], cX[v])     # cut when v is X
        if cI[v]: flow.edge(index[v], T, cI[v])     # cut when v is IALU
    for (a, b), w in pairs.items():
        flow.edge(index[a], index[b], w, w)
    flow.maxflow(S, T)
    side = flow.source_side(S)
    return {v: ('I' if index[v] in side else 'X') for v in order}


def _short(k):
    return isinstance(k, int) and -128 <= s32(k) <= 127


def rebase_offsets(fn):
    """Take extension words out of load/store offsets.

    An offset outside -128..127 needs an extension word, and a packet holds
    only one. An access `[p + q + k]` whose address add feeds only such
    accesses becomes the indexed `[p + (q + k)]` (no add, no extension):
    the anchor `q + k` is computed once after q's definition, shared by
    every access with the same q and k and outside a loop when q is
    invariant. q is the operand with more uses (a state or array base)."""
    uses = defaultdict(list)
    where = {}
    for b in fn.blocks:
        for p in b.phis:
            where[p.d] = (b, None)
            for x in p.a: uses[x].append(p)
        for i in b.ins:
            if i.d is not None: where[i.d] = (b, i)
            for x in i.a: uses[x].append(i)

    def far(u, a):
        return u.op in ('load', 'store') and u.a[0] == a and (u.op == 'load' or u.a[1] != a) \
            and isinstance(u.imm, int) and not _short(u.imm)

    def inert(v):
        d = where.get(v)
        return d is None or (d[1] is not None and d[1].op in ('const', 'sym', 'frame'))

    after = defaultdict(list)          # id(def ins) or ('phis', block id) -> new ins
    made = {}
    changed = False
    for b in fn.blocks:
        for i in b.ins:
            if i.op not in ('load', 'store') or not isinstance(i.imm, int) or _short(i.imm): continue
            a = i.a[0]
            d = where.get(a, (None, None))[1]
            if d is None or d.op != 'add' or any(inert(x) for x in d.a) or not all(far(u, a) for u in uses[a]):
                continue
            p, q = d.a
            if len(uses[p]) > len(uses[q]): p, q = q, p
            key = (q, i.imm & MASK)
            n = made.get(key)
            if n is None:
                c = Ins('const', fn.vreg(), (), i.imm & MASK)
                n = made[key] = fn.vreg()
                qb, qd = where[q]
                new = [c, Ins('add', n, [q, c.d])]
                for x in new: x.blk = qb
                after[id(qd) if qd is not None else ('phis', qb.id)].extend(new)
            key = ('idx', id(d), p, n)
            m = made.get(key)
            if m is None:
                m = made[key] = fn.vreg()
                new = Ins('add', m, [p, n]); new.blk = d.blk
                after[id(d)].append(new)
            i.a[0] = m; i.imm = 0
            changed = True
    if not changed: return False
    for b in fn.blocks:
        out = list(after.get(('phis', b.id), []))
        for i in b.ins:
            out.append(i)
            out.extend(after.get(id(i), ()))
        b.ins = out
    return True


def _argument_load(d, defs, fn=None):
    """Is instruction d the entry load of a promoted (never written) argument?"""
    if d is None or d.op != 'load' or d.vol or d.imm: return False
    f = defs.get(d.a[0])
    if f is None or f.op != 'frame': return False
    return fn is None or fn.frame[f.imm].kind == 'arg'


def split_ialu(fn, defs, cls):
    """Partition IALU values into J and K components, balancing unit load.

    The partition that cuts at hubs (see _split_ialu) is used only when its
    estimated busiest-unit load is clearly lower; otherwise hub copies would
    only add IALU work."""
    from .opt import DISABLED
    out, jl, kl = _split_ialu(fn, defs, cls, False)
    if 'hub' not in DISABLED:
        cost = lambda j, k: sum(max(j.get(b, 0.0), k.get(b, 0.0)) for b in set(j) | set(k))
        h = _split_ialu(fn, defs, cls, True)
        before, after = cost(jl, kl), cost(h[1], h[2])
        if before - after >= 1:
            out, jl, kl = h
    # Remember per-block unit load so fix-up conversions can pick the idler IALU.
    fn.ialu_load = (jl, kl)
    return out


def _split_ialu(fn, defs, cls, use_hubs):
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x]); x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb

    def ialu(v): return cls.get(v) == 'I'
    load = defaultdict(lambda: defaultdict(float))   # component -> block -> weight
    # How hot is each value as a memory address? Cold operations must not merge
    # two hot address components (e.g. an alignment test of two copy pointers):
    # they get a conversion move instead.
    hot = defaultdict(float)
    for b in fn.blocks:
        for i in b.ins:
            if i.op in ('load', 'store', 'loadq', 'storeq'):
                hot[i.a[0]] = max(hot[i.a[0]], b.freq)
    for b in fn.blocks:
        for p in b.phis:
            for x in p.a: hot[p.d] = max(hot[p.d], hot[x]); hot[x] = max(hot[x], hot[p.d])
    # Unions from the hottest operations first. A colder operation must not merge
    # two components that are each hot address streams (e.g. a run-time overlap
    # test of two loop pointers): it gets a conversion move instead.
    comp_hot = {}

    def join(a, b, f, force=False):
        ra, rb = find(a), find(b)
        if ra == rb: return
        ha, hb = comp_hot.get(ra, hot[ra]), comp_hot.get(rb, hot[rb])
        if not force and ha > 4*f and hb > 4*f: return
        parent[ra] = rb
        comp_hot[rb] = max(ha, hb)
    # Hubs: non-phi IALU values that feed three or more IALU operations (a
    # state pointer, a ring-buffer write index). Their users are not merged
    # into one component through them, so address chains hanging off one
    # hub can go to either IALU; a user on the hub's other unit reads a copy
    # made once after the hub's definition (fixup).
    hubs = set()
    if use_hubs:
        nuse = defaultdict(int)
        for b in fn.blocks:
            for i in b.ins:
                if i.op in FLEX and ialu(i.d): xs = i.a
                elif i.op in ('load', 'store', 'loadq', 'storeq'): xs = i.a[:1]
                else: continue
                for x in set(xs):
                    if ialu(x): nuse[x] += 1
        hubs = {v for v, n in nuse.items() if n >= 3 and defs.get(v) is not None and defs[v].op != 'phi'}
    cands = []
    for b in fn.blocks:
        for p in b.phis:
            if ialu(p.d):
                for x in p.a:
                    if ialu(x): cands.append((b.freq, 0, p.d, x))
        for i in b.ins:
            op = i.op
            if op in FLEX or op == 'copy':
                if ialu(i.d):
                    xs = [x for x in i.a if ialu(x)]
                    if len(xs) >= 2 and op != 'copy' and all(hot[x] > 4*b.freq for x in xs):
                        continue
                    for x in xs:
                        if op != 'copy' and x in hubs: continue
                        cands.append((b.freq, 1, i.d, x))
            elif op in ('cmp', 'br', 'sel'):
                x, y = i.a[0], i.a[1]
                if ialu(x) and ialu(y): cands.append((b.freq, 2, x, y))
    cands.sort(key=lambda c: (-c[0], c[1]))
    for f, kind, x, y in cands:
        join(x, y, f, force=kind == 0)
    # SP-relative frame addressing and argument stores always use J.
    forced_j = set()
    for b in fn.blocks:
        f = b.freq
        for i in b.ins:
            op = i.op
            if op in ('load', 'store', 'jtab') and ialu(i.a[0]):
                load[find(i.a[0])][b.id] += f
            elif op in ('loadq', 'storeq') and ialu(i.a[0]):
                load[find(i.a[0])][b.id] += f
            elif op in FLEX and ialu(i.d):
                load[find(i.d)][b.id] += f
                # X operands must first be moved into this component's IALU.
                load[find(i.d)][b.id] += f * sum(1 for x in i.a if cls.get(x) == 'X')
            elif op in ('cmp', 'br') and ialu(i.a[0]):
                load[find(i.a[0])][b.id] += f
            elif op == 'frame' and ialu(i.d):
                load[find(i.d)][b.id] += f
                forced_j.add(find(i.d))   # derived from J27
            elif op == 'call' and i.s is None and ialu(i.a[0]):
                forced_j.add(find(i.a[0]))
            if op == 'load' and ialu(i.d) or op == 'store' and ialu(i.a[1]):
                load[find(i.d if op == 'load' else i.a[1])][b.id] += 0.5*f
    base_j = defaultdict(float)
    for b in fn.blocks:
        for i in b.ins:
            if i.op == 'call':
                base_j[b.id] += (len(i.a)+1)*b.freq
            elif i.op == 'load' and defs.get(i.a[0]) is not None and defs[i.a[0]].op == 'frame':
                base_j[b.id] += b.freq
    comps = defaultdict(list)
    for v, c in cls.items():
        if c == 'I': comps[find(v)].append(v)
    jl = defaultdict(float, base_j); kl = defaultdict(float)
    unit = {}
    # Hubs a component reads through a cut edge; hub components go first so
    # a user knows whether it needs a copy of the hub on its unit.
    needs = defaultdict(set)
    for b in fn.blocks:
        for i in b.ins:
            if i.op in FLEX and ialu(i.d):
                for x in i.a:
                    if x in hubs and find(x) != find(i.d): needs[find(i.d)].add(x)
    hub_comps = {find(h) for h in hubs}
    copied = set()

    def copy_cost(h, u):
        """(unit, block, weight) of the conversion giving hub h to unit u, or None."""
        if unit.get(find(h), u) == u or (h, u) in copied: return None
        d = defs[h]
        blk = d.blk if d.blk is not None else fn.entry
        # An argument is loaded again (a J-IALU frame load); other hubs are copied
        # by a register transfer, which the packet model charges to the K-IALU.
        return ('J' if _argument_load(d, defs, fn) else 'K', blk.id, blk.freq)

    ranked = sorted(comps, key=lambda r: (r not in hub_comps, -sum(load[r].values())))
    for r in ranked:
        w = load[r]
        if r in forced_j:
            unit[r] = 'J'
        else:
            def delta(u):
                jx, kx = defaultdict(float), defaultdict(float)
                for b, x in w.items(): (jx if u == 'J' else kx)[b] += x
                for h in needs.get(r, ()):
                    c = copy_cost(h, u)
                    if c: (jx if c[0] == 'J' else kx)[c[1]] += c[2]
                return sum(max(jl[b]+jx[b], kl[b]+kx[b]) - max(jl[b], kl[b]) for b in set(jx) | set(kx))
            dj, dk = delta('J'), delta('K')
            unit[r] = 'J' if dj <= dk else 'K'
        tgt = jl if unit[r] == 'J' else kl
        for b, x in w.items(): tgt[b] += x
        for h in needs.get(r, ()):
            c = copy_cost(h, unit[r])
            if c:
                (jl if c[0] == 'J' else kl)[c[1]] += c[2]
                copied.add((h, unit[r]))
    out = {}
    for r, vs in comps.items():
        for v in vs: out[v] = unit[r]
    for v, c in cls.items():
        if c == 'X': out[v] = 'X'
    return out, jl, kl


# ---------------------------------------------------------------------------
# second compute block

Y_OPS = {'add', 'sub', 'and', 'or', 'xor', 'andn', 'min', 'max', 'neg', 'not', 'abs', 'shl', 'shr', 'sar', 'mul', 'mulfr'}


def _cunit(op):
    return 'sh' if op in ('shl', 'shr', 'sar', 'clz', 'fext') else 'mul' if op in ('mul', 'mulhs', 'mulhu', 'mulq16', 'mulfr') else 'alu'


def split_compute(fn, defs, cls):
    """Give compute block Y part of the X work of hot single-block loops.

    Each compute block has its own ALU, shifter and multiplier, so a loop
    limited by one X unit can run up to twice as fast. Operations are assigned
    greedily (in program order, so independent unrolled copies alternate) to
    the block that minimises the estimated packets per iteration. A value
    needed in the other block is recomputed there when it is a cheap function
    of loop phis and invariants (e.g. an induction update), otherwise it is
    transferred (an IALU operation). Values from outside the loop are copied
    once outside it.
    """
    from .opt import DISABLED
    if 'ysplit' in DISABLED: return False
    changed = False
    for B in list(fn.blocks):
        if B.freq < 8 or B not in B.succs: continue
        if _split_block(fn, B, defs, cls): changed = True
    return changed


def _split_block(fn, B, defs, cls):
    inside = {i.d: i for i in B.ins if i.d is not None}
    phis = {p.d: p for p in B.phis}
    X = lambda v: cls.get(v) == 'X'
    load = {'X': defaultdict(int), 'Y': defaultdict(int)}
    ialu = 0
    movable = []
    for i in B.ins:
        if i.op in ('load', 'store', 'loadq', 'storeq'):
            ialu += 1
            base = defs.get(i.a[0])
            if base is not None and (X(i.a[0]) or base.op == 'add' and any(X(x) for x in base.a)): ialu += 1
        elif i.d is not None and cls.get(i.d) in ('J', 'K') and i.op not in ('const', 'sym'):
            ialu += 1
        elif i.d is not None and X(i.d) and i.op not in ('const', 'sym', 'copy'):
            if i.op in Y_OPS: movable.append(i)
            else: load['X'][_cunit(i.op)] += 1
        elif i.op in ('br', 'sel', 'cmp') and any(X(x) for x in i.a[:2]):
            load['X']['alu'] += 1
    total = defaultdict(int)
    for i in movable: total[_cunit(i.op)] += 1
    xbound = max((load['X'][u] + total[u] for u in ('alu', 'sh', 'mul')), default=0)
    if xbound <= (ialu + 1) // 2 + 1 or len(movable) < 4: return False
    # Components: movable operations connected through values computed in the
    # loop (phis, invariants and loads are boundaries). A component (e.g. one
    # unrolled copy's address computation) goes to one block as a whole.
    mov = {i.d: i for i in movable}
    parent = {v: v for v in mov}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in movable:
        for x in i.a:
            if x in mov: parent[find(x)] = find(i.d)
    users = defaultdict(list)
    for i in B.ins:
        for x in i.a: users[x].append(i)
    latch_phis = defaultdict(list)
    for p in B.phis:
        for pred, x in zip(p.t, p.a):
            if pred is B: latch_phis[x].append(p)
    comps = defaultdict(list)
    for i in movable: comps[find(i.d)].append(i)
    for p in B.phis:
        for x in p.a: users[x].append(p)

    def carrier(p):
        """A phi whose uses only move its value (addresses, stores, IALU adds)."""
        for u in users.get(p.d, ()):
            if u.op in ('load', 'store') and (u.a[0] == p.d or u.op == 'store'): continue
            if u.op == 'add' and cls.get(u.d) in ('J', 'K'): continue
            return False
        return True
    pinned = set()
    follow = {}     # carrier phi -> component root
    for r, ops in comps.items():
        for i in ops:
            for u in users.get(i.d, ()):
                if u.op == 'phi': continue
                if u.d is not None and u.d not in mov and X(u.d) and u.op not in ('load', 'store'):
                    pinned.add(r)
                elif u.op in ('br', 'sel', 'cmp'):
                    pinned.add(r)
            for p in latch_phis.get(i.d, ()):
                if carrier(p): follow[p.d] = r
                elif not all(x in phis or x not in inside for x in i.a):
                    # Carried into other work: must be cheap to recompute on the other side.
                    pinned.add(r)
    side = {}
    phi_sides = defaultdict(set)
    for p in phis:
        if X(p): phi_sides[p].add('X')

    def needs(ops, s):
        """Phis this component uses that do not yet exist in block s."""
        return {x for i in ops for x in i.a if x in phis and s not in phi_sides[x]}
    order = sorted(comps, key=lambda r: (r in pinned, -len(comps[r])), reverse=False)
    order = [r for r in order if r in pinned] + [r for r in order if r not in pinned]
    for r in order:
        ops = comps[r]
        cnt = defaultdict(int)
        for i in ops: cnt[_cunit(i.op)] += 1
        best = None
        for s in (('X',) if r in pinned else ('X', 'Y')):
            dup = len(needs(ops, s))
            worst = max(load[s][u] + cnt[u] + (dup if u == 'alu' else 0) for u in ('alu', 'sh', 'mul'))
            other = max(load['Y' if s == 'X' else 'X'][u] for u in ('alu', 'sh', 'mul'))
            cost = (max(worst, other), sum(load[s].values()) + len(ops) + dup, s != 'X')
            if best is None or cost < best[0]: best = (cost, s, dup)
        _, s, dup = best
        for u, c_ in cnt.items(): load[s][u] += c_
        load[s]['alu'] += dup
        for i in ops:
            side[i.d] = s
            for x in i.a:
                if x in phis: phi_sides[x].add(s)
    if not any(s == 'Y' for s in side.values()): return False
    # --- materialise ------------------------------------------------------
    for v, s in side.items(): cls[v] = s
    for p, r in follow.items():
        if cls.get(p) in ('X', 'Y'): cls[p] = side[comps[r][0].d]
    orig = {i.d: list(i.a) for i in B.ins if i.d is not None}
    # Loads feeding only Y work load straight into Y registers.
    users = defaultdict(list)
    for i in B.ins:
        for x in i.a: users[x].append(i)
    for p in B.phis:
        for x in p.a: users[x].append(p)
    for i in B.ins:
        if i.op == 'load' and X(i.d) and users[i.d] and all(u.d is not None and side.get(u.d) == 'Y' for u in users[i.d]):
            cls[i.d] = 'Y'
    versions = {}

    def version(v, s):
        """v in compute block s (the original when it cannot be recomputed cheaply)."""
        if cls.get(v) == s or (v not in inside and v not in phis): return v
        key = (v, s)
        if key in versions: return versions[key]
        if v in phis:
            p = phis[v]
            if cls.get(v) not in ('X', 'Y'): return v
            q = Ins('phi', fn.vreg(), list(p.a)); q.t = list(p.t); q.blk = B
            cls[q.d] = s; versions[key] = q.d; B.phis.append(q); defs[q.d] = q
            q.a = [version(x, s) if pred is B else x for pred, x in zip(p.t, p.a)]
            return q.d
        i = inside[v]
        args = orig.get(v, i.a)   # operands before this pass rewrote them
        # Only direct functions of phis and invariants (induction updates) are
        # recomputed; anything deeper is transferred.
        if i.op not in Y_OPS or cls.get(v) not in ('X', 'Y') or any(x in inside for x in args):
            return v
        versions[key] = n = fn.vreg()
        c = Ins(i.op, n, [version(x, s) for x in args], i.imm, i.s); c.blk = B
        cls[n] = s; defs[n] = c
        B.ins.insert(B.ins.index(i) + 1, c)
        return n
    for i in list(B.ins):
        if i.d is None or i.d not in side: continue
        s = side[i.d]
        i.a = [version(x, s) for x in i.a]
    # Phis must receive their loop value in their own block.
    for p in list(B.phis):
        s = cls.get(p.d)
        if s in ('X', 'Y'):
            p.a = [version(x, s) if pred is B else x for pred, x in zip(p.t, p.a)]
    return True


def _cheap(v, inside, phis, depth=0):
    """Recomputable from phis, invariants and constants by a short chain."""
    if v in phis or v not in inside: return True
    i = inside[v]
    if depth > 2 or i.op not in Y_OPS: return False
    return all(_cheap(x, inside, phis, depth+1) for x in i.a)


# ---------------------------------------------------------------------------
# class fix-ups on SSA: every operand is available in the class its user needs

def operand_needs(i, cls, defs):
    """[(operand index, required class or None)]; None means any class."""
    op = i.op; a = i.a
    if op == 'load': return [(0, 'IALU')]
    if op == 'loadq': return [(0, 'IALU')]
    if op == 'storeq': return [(0, 'IALU'), (1, 'X')]
    if op == 'store': return [(0, 'IALU'), (1, None)]
    if op in FLEX:
        c = cls.get(i.d, 'X')
        return [(k, c) for k in range(len(a))]
    if op in XONLY:
        c = 'Y' if cls.get(i.d) == 'Y' else 'X'
        return [(k, c) for k in range(len(a))]
    if op in DIVS: return [(0, 'X'), (1, 'X')]
    if op in ('cmp', 'br', 'sel'):
        x, y = a[0], a[1]
        c = cls.get(x) if not _is_const(defs, x) else cls.get(y)
        if c is None: c = 'J'
        if c == 'Y': c = 'X'   # compares and conditions use the X block
        if op == 'sel':
            return [(0, c), (1, c), (2, None), (3, None)]
        return [(0, c), (1, c)]
    if op == 'call':
        # Indirect targets feed `Jn = Jt + 0 (CJMP)`, a J-IALU operation.
        if i.s is None: return [(0, 'J')] + [(k, None) for k in range(1, len(a))]
        return [(k, None) for k in range(len(a))]
    if op == 'jtab': return [(0, 'J')]
    if op == 'ret': return [(0, None)] if a else []
    if op == 'copy': return [(0, None)]
    return [(k, None) for k in range(len(a))]


def imm_ok(i, k, defs, cls):
    """Can operand k of IR instruction i be encoded as an immediate?"""
    op = i.op
    if op in ('shl', 'shr', 'sar') and k == 1: return True
    if op in ('add', 'sub', 'and', 'or', 'xor', 'andn') and k == 1 and cls.get(i.d) in ('J', 'K'): return True
    if op in ('cmp', 'br', 'sel') and k == 1:
        x = i.a[0]
        return not _is_const(defs, x) and cls.get(x) in ('J', 'K')
    if op == 'sel' and k in (2, 3):
        return True   # constants load straight into the result register
    return False


def fixup(fn, defs, cls):
    """Insert cross-class copies and per-class constant materializations."""
    converted = {}
    consts = {}
    insert_after = defaultdict(list)   # def ins id -> [new ins]
    phi_insert = defaultdict(list)     # block id -> [ins at block start]

    jl, kl = getattr(fn, 'ialu_load', ({}, {}))
    current = [None]

    def resolve(req, v):
        if req == 'IALU':
            if cls.get(v) in ('J', 'K'): return cls[v]
            b = current[0]
            if b is None: return 'J'
            # An X value needed as an address: move it into the less loaded IALU.
            f = b.freq
            if kl.get(b.id, 0.0) + f <= jl.get(b.id, 0.0):
                kl[b.id] = kl.get(b.id, 0.0) + 2*f; return 'K'
            jl[b.id] = jl.get(b.id, 0.0) + 2*f; return 'J'
        return req

    def materialize(v, c):
        key = (v, c)
        n = consts.get(key)
        if n is None:
            src = defs[v]
            n = fn.vreg(); cls[n] = c
            new = Ins(src.op, n, (), src.imm, src.s)
            insert_after[id(src)].append(new)
            consts[key] = n
        return n

    def convert(v, c):
        src = defs.get(v)
        b = current[0]
        # A colder block converts for itself instead of burdening a hot definition.
        local = src is not None and b is not None and src.blk is not None and src.blk is not b \
            and src.blk.freq > b.freq
        key = (v, c, b.id) if local else (v, c)
        n = converted.get(key)
        if n is None:
            n = fn.vreg(); cls[n] = c
            new = Ins('copy', n, [v])
            if not local and c in ('J', 'K') and _argument_load(src, defs, fn):
                # A second IALU copy of an argument: load it again (J-IALU frame
                # load) instead of a register transfer, independent of the first.
                new = Ins('load', n, [src.a[0]], src.imm, src.s)
            if local:
                phi_insert[b.id].append(new)
            elif src is None or src.op == 'phi':
                blk = src.blk if src is not None else fn.entry
                phi_insert[blk.id].append(new)
            else:
                insert_after[id(src)].append(new)
            converted[key] = n
        return n

    for b in fn.blocks:
        current[0] = b
        for p in b.phis:
            c = cls.get(p.d)
            for k, (pred, v) in enumerate(zip(p.t, p.a)):
                d = defs.get(v)
                if c is None or _is_const(defs, v) or d is None or d.op == 'phi' or d.blk is not pred: continue
                if cls.get(v) in REG_CLASSES and cls.get(v) != c:
                    # The value must be converted in the predecessor (where it is defined).
                    current[0] = pred
                    p.a[k] = convert(v, c)
                    current[0] = b
        for i in b.ins:
            for k, req in operand_needs(i, cls, defs):
                v = i.a[k]
                if _is_const(defs, v):
                    if imm_ok(i, k, defs, cls): continue
                    if i.op in ('load', 'store') and k == 0 and defs[v].op == 'sym':
                        c = 'J'
                    else:
                        c = resolve(req, v) if req is not None else 'X'
                        if c == 'IALU': c = 'J'
                    i.a[k] = materialize(v, c)
                    continue
                if req is None: continue
                have = cls.get(v)
                need = resolve(req, v)
                if have == need or (req == 'IALU' and have in ('J', 'K')): continue
                i.a[k] = convert(v, need)
        for p in b.phis:
            for k, v in enumerate(p.a):
                if _is_const(defs, v):
                    p.a[k] = materialize(v, cls[p.d])
    for b in fn.blocks:
        out = []
        start = phi_insert.get(b.id, [])
        for n in start: n.blk = b
        out.extend(start)
        for i in b.ins:
            out.append(i)
            extra = insert_after.get(id(i))
            if extra:
                for n in extra: n.blk = b
                out.extend(extra)
        b.ins = out
    # Remaining constant defs keep a class for register materialization.
    for b in fn.blocks:
        for i in b.ins:
            if i.op in ('const', 'sym') and i.d not in cls:
                cls[i.d] = 'X' if i.op == 'const' else 'J'
    return cls


# ---------------------------------------------------------------------------
# compute-unit balancing

X_ALU = {'add', 'sub', 'and', 'or', 'xor', 'andn', 'min', 'max', 'neg', 'not', 'abs', 'sel', 'cmp'}
X_SHIFT = {'shl', 'shr', 'sar', 'clz'}


def form_fext(fn, cls):
    """`(x >> k) & (2^n - 1)` (k + n <= 32, compute blocks) -> one shifter
    FEXT with control word k<<8 | n (PR 6-5), defined next to the mask."""
    defs = {}
    uses = defaultdict(int)
    for b in fn.blocks:
        for i in b.ins:
            if i.d is not None: defs[i.d] = i
            for x in i.a: uses[x] += 1
        for p in b.phis:
            for x in p.a: uses[x] += 1
    changed = False
    ctl = {}
    for b in fn.blocks:
        for i in b.ins:
            if i.op != 'and' or cls.get(i.d) not in ('X', 'Y'): continue
            for mi_, ti_ in ((1, 0), (0, 1)):
                m, t = defs.get(i.a[mi_]), defs.get(i.a[ti_])
                if m is None or m.op != 'const' or t is None or t.op not in ('shr', 'sar'): continue
                if cls.get(t.d) != cls.get(i.d) or uses[t.d] != 1: continue
                mask = m.imm & MASK
                if mask == 0 or mask & (mask + 1): continue
                k = defs.get(t.a[1])
                if k is None or k.op != 'const' or k.imm & MASK > 31: continue
                a_, n_ = k.imm & 31, mask.bit_length()
                if a_ + n_ > 32 or n_ == 32: continue
                key = (m.blk.id, (a_ << 8) | n_)
                c = ctl.get(key)
                if c is None:
                    c = ctl[key] = Ins('const', fn.vreg(), (), (a_ << 8) | n_); c.blk = m.blk
                    m.blk.ins.insert(m.blk.ins.index(m) + 1, c)
                    defs[c.d] = c
                i.op = 'fext'; i.a = [t.a[0], c.d]
                changed = True
                break
    return changed


def form_clip(fn, cls):
    """A symmetric compute-block clamp `min(max(x, -c), c)` (either order,
    constant 0 <= c < 2^31) becomes one `CLIP x BY c`, which returns x when
    |x| < |c| and otherwise |c| with x's sign (PR 10-25): one ALU operation
    on the dependency chain instead of two."""
    defs = {}
    uses = defaultdict(int)
    for b in fn.blocks:
        for i in b.ins:
            if i.d is not None: defs[i.d] = i
            for x in i.a: uses[x] += 1
        for p in b.phis:
            for x in p.a: uses[x] += 1

    def const(v):
        d = defs.get(v)
        return s32(d.imm) if d is not None and d.op == 'const' else None
    changed = False
    for b in fn.blocks:
        for i in b.ins:
            if i.op not in ('min', 'max') or cls.get(i.d) not in ('X', 'Y'): continue
            inner = 'max' if i.op == 'min' else 'min'
            for k in (0, 1):
                t, c = defs.get(i.a[k]), const(i.a[1-k])
                if c is None or t is None or t.op != inner or uses[t.d] != 1 or cls.get(t.d) != cls.get(i.d): continue
                for j in (0, 1):
                    e = const(t.a[1-j])
                    if e is None: continue
                    hi, lo = (c, e) if i.op == 'min' else (e, c)
                    if hi >= 0 and lo == -hi:
                        hv = i.a[1-k] if i.op == 'min' else t.a[1-j]
                        i.op = 'clip'; i.a = [t.a[j], hv]
                        changed = True
                        break
                if i.op == 'clip': break
    return changed


def balance_units(fn, cls):
    """In hot blocks whose X ALU work exceeds the shifter's, move bit-field
    extractions `(x >> a) & (2^n - 1)` onto the shifter as
    `(x << (32-a-n)) >>> (32-n)` (two shifts, no ALU operation)."""
    defs = {}
    uses = defaultdict(int)
    for b in fn.blocks:
        for i in b.ins:
            if i.d is not None: defs[i.d] = i
            for x in i.a: uses[x] += 1
        for p in b.phis:
            for x in p.a: uses[x] += 1
    changed = False
    for b in fn.blocks:
        if b.freq < 8: continue
        alu = sum(1 for i in b.ins if i.op in X_ALU and cls.get(i.d) == 'X')
        sh = sum(1 for i in b.ins if i.op in X_SHIFT and cls.get(i.d) == 'X')
        for i in b.ins:
            if alu <= sh + 1: break
            if i.op != 'and' or cls.get(i.d) != 'X': continue
            m = defs.get(i.a[1])
            t = defs.get(i.a[0])
            if m is None or m.op != 'const' or t is None or t.op not in ('shr', 'sar') or t.blk is not b: continue
            mask = m.imm & MASK
            if mask == 0 or mask & (mask + 1): continue
            k = defs.get(t.a[1])
            if k is None or k.op != 'const' or uses[t.d] != 1: continue
            a_ = k.imm & 31; n_ = mask.bit_length()
            if a_ + n_ > 32 or n_ == 32: continue
            # t: x << (32-a-n) ; i: t >>> (32-n)
            c1 = Ins('const', fn.vreg(), (), 32 - a_ - n_); c1.blk = b
            c2 = Ins('const', fn.vreg(), (), 32 - n_); c2.blk = b
            cls[c1.d] = cls[c2.d] = 'X'
            pos = b.ins.index(t)
            b.ins[pos:pos] = [c1, c2]
            t.op = 'shl'; t.a = [t.a[0], c1.d]
            i.op = 'shr'; i.a = [t.d, c2.d]
            alu -= 1; sh += 1
            changed = True
    return changed


# ---------------------------------------------------------------------------
# machine IR

class _Private:
    """Address of frame traffic no pointer can reach: outgoing and tail-call
    arguments, spills, saves and never-addressed frame slots. The scheduler
    orders such accesses only against other frame accesses, by SP offset."""
    kind = 'private'


PRIVATE = _Private()


class MI:
    __slots__ = ('op', 'd', 'u', 'imm', 's', 't', 'addr', 'vol', 'extra')

    def __init__(self, op, d=(), u=(), imm=None, s=None, t=(), addr=None, vol=False, extra=None):
        self.op = op; self.d = list(d); self.u = list(u); self.imm = imm; self.s = s
        self.t = list(t); self.addr = addr; self.vol = vol; self.extra = extra

    def __repr__(self):
        return f'{self.op} d={self.d} u={self.u} imm={self.imm} s={self.s} t={[getattr(x, "id", x) for x in self.t]}'


class MBlock:
    __slots__ = ('id', 'mis', 'succs', 'preds', 'freq', 'depth', 'label', 'ir')

    def __init__(self, b):
        self.id = b.id; self.mis = []; self.succs = []; self.preds = []
        self.freq = b.freq; self.depth = b.depth; self.label = None; self.ir = b


class MFunction:
    def __init__(self, fn):
        self.fn = fn
        self.blocks = []
        self.cls = {}
        self.width = {}
        self.part = {}
        self.frame = fn.frame
        self.max_args = 0
        self.leaf = True
        self.jtabs = []
        self.nv = fn.nv

    def vreg(self, c, width=1, part='lo'):
        self.fn.nv += 1
        v = self.fn.nv
        self.cls[v] = c
        if width != 1: self.width[v] = width; self.part[v] = part
        return v


COND_J = {'eq': 'JEQ', 'ne': 'NJEQ', 'lt': 'JLT', 'le': 'JLE', 'gt': 'NJLE', 'ge': 'NJLT'}
COND_X = {'eq': 'XAEQ', 'ne': 'NXAEQ', 'lt': 'XALT', 'le': 'XALE', 'gt': 'NXALE', 'ge': 'NXALT'}


def condition(cc, c):
    base = cc[:-1] if cc.endswith('u') and cc not in ('eq', 'ne') else cc
    unsigned = base != cc
    if c == 'X': name = COND_X[base]
    else:
        name = COND_J[base]
        if c == 'K': name = name.replace('J', 'K')
    return name, unsigned


def select(fn, defs, cls, addr_of):
    """Translate non-SSA IR (after phi removal) into machine IR over vregs."""
    mf = MFunction(fn)
    mf.cls = cls
    blocks = {}
    for b in fn.blocks:
        mb = MBlock(b); blocks[b.id] = mb; mf.blocks.append(mb)
    for b in fn.blocks:
        mb = blocks[b.id]
        mb.succs = [blocks[s.id] for s in b.succs]
    for mb in mf.blocks:
        for s in mb.succs: s.preds.append(mb)
    constval = {v: i for v, i in defs.items() if i.op in ('const', 'sym')}
    # Quad-wide values: loadq results and anything copied from them.
    quad = set()
    for b in fn.blocks:
        for i in b.ins:
            if i.op == 'loadq': quad.add(i.d)
    grew = True
    while grew:
        grew = False
        for b in fn.blocks:
            for i in b.ins:
                if i.op == 'copy' and (i.a[0] in quad) != (i.d in quad):
                    quad.add(i.a[0]); quad.add(i.d); grew = True
    for v in quad:
        mf.width[v] = 4; mf.cls[v] = 'X'

    for b in fn.blocks:
        mb = blocks[b.id]
        out = mb.mis
        # Pair up division and remainder of identical operands.
        divpairs = {}
        for i in b.ins:
            if i.op in DIVS:
                key = ('s' if i.op in ('div', 'mod') else 'u', tuple(i.a))
                divpairs.setdefault(key, []).append(i)
        done_div = set()

        def c_of(v):
            return cls.get(v, 'X')

        def li(v, dest=None):
            src = constval[v]
            d = dest if dest is not None else v
            if src.op == 'sym': out.append(MI('li', [d], [], src.imm, src.s))
            else: out.append(MI('li', [d], [], src.imm & MASK))
            return d

        def const_of(v):
            i = constval.get(v)
            return None if i is None or i.op != 'const' else i.imm & MASK

        def imm_of(v):
            """Immediate operand for a constant or symbol-address vreg."""
            i = constval[v]
            if i.op == 'const': return i.imm & MASK
            off = s32(i.imm)
            return i.s + (f' + {off}' if off > 0 else f' - {-off}' if off < 0 else '')

        for i in b.ins:
            op = i.op; a = i.a; d = i.d
            final = None
            if d is not None and cls.get(d, 'X') not in ('X', 'Y') and op in XONLY:
                # X-only operation whose result lives in an IALU register.
                final = d
                d = mf.vreg('X')
            _select_one(i, op, a, d, out, mf, cls, defs, constval, blocks, addr_of, divpairs, done_div, li, const_of, imm_of, c_of)
            if final is not None:
                out.append(MI('mov', [final], [d]))
    return mf


def _select_one(i, op, a, d, out, mf, cls, defs, constval, blocks, addr_of, divpairs, done_div, li, const_of, imm_of, c_of):
    """Select machine instructions for one IR instruction (appends to `out`)."""
    if op in ('const', 'sym'):
        li(d); return
    if op == 'copy':
        if a[0] in constval and constval[a[0]].op in ('const', 'sym'):
            li(a[0], d)
        else:
            out.append(MI('mov', [d], [a[0]], None, 'Q' if mf.width.get(d, 1) == 4 else None))
        return
    if op == 'frame':
        # Frame addresses are J27-relative, a J-IALU computation; other classes copy it.
        if c_of(d) == 'J':
            out.append(MI('ialu', [d], ['J27'], 0, '+', extra=('frame', i.imm)))
        else:
            t = mf.vreg('J')
            out.append(MI('ialu', [t], ['J27'], 0, '+', extra=('frame', i.imm)))
            out.append(MI('mov', [d], [t]))
        return
    if op in IALU_OPS or op in ('min', 'max', 'neg', 'not', 'abs'):
        c = c_of(d)
        if c in ('J', 'K'):
            z = 'J31' if c == 'J' else 'K31'
            if op == 'neg': out.append(MI('ialu', [d], [z, a[0]], None, '-')); return
            if op == 'abs':
                # Preserve the explicit MAX(x, 0 - x) expansion for IALU abs.
                t = mf.vreg(c)
                out.append(MI('ialu', [t], [z, a[0]], None, '-'))
                out.append(MI('ialu', [d], [a[0], t], None, 'MAX')); return
            if op == 'not': out.append(MI('ialu', [d], [a[0]], MASK, 'XOR')); return
            if op in ('min', 'max'):
                out.append(MI('ialu', [d], [a[0], a[1]], None, op.upper())); return
            x, y = a
            if x in constval and op == 'sub':
                t = mf.vreg(c); li(x, t); x = t
            if y in constval:
                out.append(MI('ialu', [d], [x], imm_of(y), IALU_OPS[op]))
            else:
                out.append(MI('ialu', [d], [x, y], None, IALU_OPS[op]))
            return
        if op == 'neg': out.append(MI('xalu', [d], [a[0]], None, 'NEG')); return
        if op == 'not': out.append(MI('xalu', [d], [a[0]], None, 'NOT')); return
        if op == 'abs': out.append(MI('xalu', [d], [a[0]], None, 'ABS')); return
        if op in ('min', 'max'): out.append(MI('xalu', [d], [a[0], a[1]], None, op.upper())); return
        k = const_of(a[1]) if op in ('add', 'sub') and a[1] in constval else None
        if k in (1, MASK):
            # x +- 1 needs no constant register: INC / DEC.
            out.append(MI('xalu', [d], [a[0]], None, 'INC' if (k == 1) == (op == 'add') else 'DEC')); return
        out.append(MI('xalu', [d], [a[0], a[1]], None, IALU_OPS[op]))
        return
    if op == 'fext':
        out.append(MI('xsh', [d], [a[0], a[1]], None, 'FEXT')); return
    if op == 'mul':
        out.append(MI('xmul', [d], [a[0], a[1]])); return
    if op == 'mulfr':
        out.append(MI('xmul', [d], [a[0], a[1]], None, 'T')); return
    if op == 'clip':
        out.append(MI('xalu', [d], [a[0], a[1]], None, 'CLIP')); return
    if op in ('shl', 'shr', 'sar'):
        kind = 'ASHIFT' if op == 'sar' else 'LSHIFT'
        k = const_of(a[1]) if a[1] in constval else None
        if k is not None:
            k = k & 63 if k < 64 else 63
            out.append(MI('xsh', [d], [a[0]], k if op == 'shl' else -k, kind))
        else:
            amt = a[1]
            if op != 'shl':
                t = mf.vreg('Y' if c_of(d) == 'Y' else 'X')
                out.append(MI('xalu', [t], [amt], None, 'NEG')); amt = t
            out.append(MI('xsh', [d], [a[0], amt], None, kind))
        return
    if op in ('mulhu', 'mulhs'):
        mf.width[d] = 2; mf.part[d] = 'hi'
        out.append(MI('xmulw', [d], [a[0], a[1]], None, 'U' if op == 'mulhu' else ''))
        return
    if op == 'mulq16':
        t = mf.vreg('X', 2, 'lo')
        out.append(MI('xmulw', [t], [a[0], a[1]], None, ''))
        mf.width[d] = 2; mf.part[d] = 'lo'
        out.append(MI('xlsh', [d], [t], -16, 'ASHIFT'))
        return
    if op == 'clz':
        out.append(MI('xclz', [d], [a[0]])); return
    if op in DIVS:
        if id(i) in done_div: return
        signed = op in ('div', 'mod')
        key = ('s' if signed else 'u', tuple(a))
        group = divpairs[key]
        qd = next((x.d for x in group if x.op in ('div', 'udiv')), None)
        rd = next((x.d for x in group if x.op in ('mod', 'umod')), None)
        for x in group: done_div.add(id(x))
        q = mf.vreg('J'); r = mf.vreg('X')
        temps = [mf.vreg('X'), mf.vreg('X'), mf.vreg('J'), mf.vreg('J')]
        if signed: temps += [mf.vreg('X'), mf.vreg('X'), mf.vreg('J'), mf.vreg('J')]
        out.append(MI('macro', [q, r]+temps, [a[0], a[1]], None, 'sdiv' if signed else 'udiv'))
        if qd is not None: out.append(MI('mov', [qd], [q]))
        if rd is not None: out.append(MI('mov', [rd], [r]))
        return
    if op == 'cmp':
        x, y = a
        c = c_of(x)
        cond, unsigned = condition(i.s, c)
        if c == 'X':
            out.append(MI('xcmp', [], [x, y], None, 'U' if unsigned else ''))
        else:
            if y in constval:
                out.append(MI('icmp', [], [x], imm_of(y), 'U' if unsigned else '', extra=c))
            else:
                out.append(MI('icmp', [], [x, y], None, 'U' if unsigned else '', extra=c))
        out.append(MI('li', [d], [], 0))
        out.append(MI('cset', [d], [d], 1, cond, extra=('X' if c == 'X' else c)))
        return
    if op == 'loadq':
        out.append(MI('ld', [d], [a[0]], i.imm, 'Q', addr=addr_of.get(id(i))))
        return
    if op == 'storeq':
        out.append(MI('st', [], [a[0], a[1]], i.imm, 'Q', addr=addr_of.get(id(i))))
        return
    if op == 'sel':
        x, y, v1, v2 = a
        c = c_of(x) if x not in constval else c_of(y)
        cond, unsigned = condition(i.s, c)
        if c == 'X':
            out.append(MI('xcmp', [], [x, y], None, 'U' if unsigned else ''))
        elif y in constval:
            out.append(MI('icmp', [], [x], imm_of(y), 'U' if unsigned else '', extra=c))
        else:
            out.append(MI('icmp', [], [x, y], None, 'U' if unsigned else '', extra=c))
        if v2 in constval: li(v2, d)
        else: out.append(MI('mov', [d], [v2]))
        if v1 in constval and constval[v1].op == 'const':
            out.append(MI('cset', [d], [d], constval[v1].imm & MASK, cond, extra=c))
        else:
            if v1 in constval:
                t = mf.vreg(c_of(d)); li(v1, t); v1 = t
            out.append(MI('cmov', [d], [d, v1], None, cond, extra=c))
        return
    if op == 'load':
        ad = addr_of.get(id(i))
        base = a[0]
        bd = defs.get(base)
        if bd is not None and bd.op == 'frame':
            out.append(MI('ld', [d], ['J27'], i.imm, addr=ad, vol=i.vol, extra=('frame', bd.imm)))
        else:
            out.append(MI('ld', [d], [base], i.imm, addr=ad, vol=i.vol))
        return
    if op == 'store':
        ad = addr_of.get(id(i))
        base, v = a
        if v in constval:
            t = mf.vreg('X'); li(v, t); v = t
        bd = defs.get(base)
        if bd is not None and bd.op == 'frame':
            out.append(MI('st', [], ['J27', v], i.imm, addr=ad, vol=i.vol, extra=('frame', bd.imm)))
        else:
            out.append(MI('st', [], [base, v], i.imm, addr=ad, vol=i.vol))
        return
    if op == 'call':
        # A call that never returns does not need the return address preserved.
        if not i.vol: mf.leaf = False
        args = a if i.s is not None else a[1:]
        mf.max_args = max(mf.max_args, (len(args)+3) & -4)
        for k, v in enumerate(args):
            if v in constval:
                t = mf.vreg('X'); li(v, t); v = t
            out.append(MI('st', [], ['J27', v], k, addr=PRIVATE, extra=('arg', k)))
        if i.s is not None:
            out.append(MI('call', [], [], None, i.s))
        else:
            t = mf.vreg('J')
            out.append(MI('ialu', [t], [a[0]], 0, '+', extra='cjmp'))
            out.append(MI('calli', [], [t]))
        if d is not None:
            out.append(MI('mov', [d], ['XR0']))
        return
    if op == 'ret':
        if a:
            v = a[0]
            if v in constval: li(v, 'XR0')
            else: out.append(MI('mov', ['XR0'], [v]))
        out.append(MI('ret', [], ['XR0'] if a else []))
        return
    if op == 'jmp':
        out.append(MI('jmp', t=[blocks[i.t[0].id]]))
        return
    if op == 'br':
        x, y = a
        c = c_of(x) if x not in constval else c_of(y)
        cond, unsigned = condition(i.s, c)
        if c == 'X':
            out.append(MI('xcmp', [], [x, y], None, 'U' if unsigned else ''))
        elif y in constval:
            out.append(MI('icmp', [], [x], imm_of(y), 'U' if unsigned else '', extra=c))
        else:
            out.append(MI('icmp', [], [x, y], None, 'U' if unsigned else '', extra=c))
        out.append(MI('br', [], [], None, cond, t=[blocks[i.t[0].id], blocks[i.t[1].id]], extra=c))
        return
    if op == 'jtab':
        mf.leaf = False
        label = f'_cc_jt_{len(mf.jtabs)}'
        mf.jtabs.append((label, [blocks[t.id] for t in i.t]))
        idx = a[0]
        if cls.get(idx) != 'J':
            t2 = mf.vreg('J'); out.append(MI('mov', [t2], [idx])); idx = t2
        # The table entry loads straight into CJMP (`CJMP = [Jidx + table]`),
        # one packet before the jump, like an epilogue's return address.
        out.append(MI('ld', ['CJMP'], [idx], label, extra='cjmp'))
        out.append(MI('ijmp', [], [], t=[blocks[t.id] for t in i.t]))
        return
    if op == 'halt':
        out.append(MI('idle')); return
    if op == 'qcopy':
        # Temps: J0 source pointer (DAB addressing needs J0..3; loads on J),
        # K destination pointer (stores on K), K odd flag, two X quads.
        # Inputs: dst, src, pairs, odd.
        temps = ['J0', mf.vreg('K'), mf.vreg('K'), mf.vreg('X', 4), mf.vreg('X', 4)]
        out.append(MI('macro', temps, list(a), None, 'qcopy'))
        return
    if op == 'setlc':
        out.append(MI('mov', [f'LC{i.imm}'], [a[0]])); return
    if op == 'lcbr':
        out.append(MI('br', [], [], None, f'NLC{i.imm}E', t=[blocks[i.t[0].id], blocks[i.t[1].id]], extra='LC'))
        return
    raise AssertionError(f'isel: unhandled IR op {op}')
