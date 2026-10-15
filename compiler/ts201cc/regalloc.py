"""Graph-colouring register allocation (Chaitin/Briggs with conservative coalescing).

Operands are vregs (ints) or physical register names (str). Allocatable
physical names appearing in code (e.g. XR0 around calls) act as precoloured
nodes. Register pairs (width 2, X only) use an even-aligned register couple.
"""
from __future__ import annotations
from collections import defaultdict
from .isel import MI

XREGS = [f'XR{i}' for i in range(32)]
JREGS = [f'J{i}' for i in range(27)] + ['J28', 'J29', 'J30']
KREGS = [f'K{i}' for i in range(31)]
YREGS = [f'YR{i}' for i in range(32)]
REGS = {'X': XREGS, 'J': JREGS, 'K': KREGS, 'Y': YREGS}
# Caller-clobbered scratch: the low half of each file (whole quad groups, so a
# callee-saved quad never holds scratch). Leaf code runs out of these without
# saving anything; values live across calls go to the callee-saved high half.
SCRATCH = {'X': [f'XR{i}' for i in range(16)], 'J': [f'J{i}' for i in range(12)],
           'K': [f'K{i}' for i in range(16)], 'Y': [f'YR{i}' for i in range(16)]}
CALLER_SAVED = set(SCRATCH['X']) | set(SCRATCH['J']) | set(SCRATCH['K']) | set(SCRATCH['Y'])
# Callee-saved order: fill aligned quad groups first (saved with one Q transfer).
CALLEE_ORDER = {'X': [f'XR{i}' for i in range(16, 32)] + SCRATCH['X'],
                'J': [f'J{i}' for i in range(12, 27)] + ['J28', 'J29', 'J30'] + SCRATCH['J'],
                'K': [f'K{i}' for i in range(16, 31)] + SCRATCH['K'],
                'Y': [f'YR{i}' for i in range(16, 32)] + SCRATCH['Y']}
ROTATION_POOL = {c: list(r) for c, r in SCRATCH.items()}
CALLER_SAVED_LIST = SCRATCH
ALLOCATABLE = set(XREGS) | set(JREGS) | set(KREGS) | set(YREGS)


def phys_class(r):
    if r.startswith('XR'): return 'X'
    if r.startswith('YR'): return 'Y'
    if r.startswith('J'): return 'J'
    if r.startswith('K'): return 'K'
    return None


def defs_uses(mi):
    """(defs, uses) including implicit effects relevant to allocation."""
    op = mi.op
    d = list(mi.d); u = list(mi.u)
    if op in ('call', 'calli'):
        d = d + sorted(CALLER_SAVED)
    elif op == 'cset':
        pass  # d already listed in u
    return d, u


def is_move(mi):
    return mi.op == 'mov' and len(mi.d) == 1 and len(mi.u) == 1


def liveness(mf):
    gen = {}; kill = {}
    for b in mf.blocks:
        g = set(); k = set()
        for mi in b.mis:
            d, u = defs_uses(mi)
            for x in u:
                if (isinstance(x, int) or x in ALLOCATABLE) and x not in k: g.add(x)
            for x in d:
                if isinstance(x, int) or x in ALLOCATABLE: k.add(x)
        gen[b.id] = g; kill[b.id] = k
    live_in = {b.id: set() for b in mf.blocks}
    live_out = {b.id: set() for b in mf.blocks}
    order = list(reversed(mf.blocks))
    changed = True
    while changed:
        changed = False
        for b in order:
            out = set()
            for s in b.succs: out |= live_in[s.id]
            inn = gen[b.id] | (out - kill[b.id])
            if inn != live_in[b.id] or out != live_out[b.id]:
                live_in[b.id] = inn; live_out[b.id] = out; changed = True
    return live_in, live_out


class Graph:
    def __init__(self):
        self.adj = defaultdict(set)
        self.forbid = defaultdict(set)   # vreg -> physical names it must avoid

    def add(self, a, b):
        if a == b: return
        ai = isinstance(a, int); bi = isinstance(b, int)
        if ai and bi:
            self.adj[a].add(b); self.adj[b].add(a)
        elif ai:
            self.forbid[a].add(b)
        elif bi:
            self.forbid[b].add(a)


def build(mf, live_out):
    g = Graph()
    cls = mf.cls
    moves = []
    cost = defaultdict(float)
    across_call = set()
    hot = mf.hot = defaultdict(float)
    for b in mf.blocks:
        live = set(live_out[b.id])
        f = b.freq
        for mi in reversed(b.mis):
            d, u = defs_uses(mi)
            mv = is_move(mi)
            if mv and (isinstance(mi.d[0], int) or isinstance(mi.u[0], int)):
                a_, b2 = (mi.d[0], mi.u[0]) if isinstance(mi.d[0], int) else (mi.u[0], mi.d[0])
                if isinstance(b2, int) or b2 in ALLOCATABLE:
                    moves.append((a_, b2, f))
            if mi.op in ('call', 'calli'):
                across_call |= {x for x in live if isinstance(x, int)}
            for x in d:
                if not (isinstance(x, int) or x in ALLOCATABLE): continue
                for y in live:
                    if mv and y == mi.u[0]: continue
                    if _same_class(cls, x, y): g.add(x, y)
                if isinstance(x, int):
                    cost[x] += f
                    if f > hot[x]: hot[x] = f
            if mi.op == 'macro':
                # Results/temporaries are written before the inputs are last read.
                for x in d:
                    for y in u:
                        if isinstance(y, int) and _same_class(cls, x, y): g.add(x, y)
                for x in d:
                    for y in d:
                        if x != y and _same_class(cls, x, y): g.add(x, y)
            for x in d:
                live.discard(x)
            for x in u:
                if isinstance(x, int) or x in ALLOCATABLE:
                    live.add(x)
                    if isinstance(x, int): cost[x] += f
    return g, moves, cost, across_call


def _same_class(cls, x, y):
    cx = cls.get(x) if isinstance(x, int) else phys_class(x)
    cy = cls.get(y) if isinstance(y, int) else phys_class(y)
    return cx is not None and cx == cy


def allocate(mf, max_rounds=12):
    no_spill = {v for v, w in mf.width.items() if w > 1}
    # Rotating registers in loops costs callee-saved saves once per call;
    # skip it for functions whose loops are no hotter than their entry.
    mf.hot_saves_costly = max((b.freq for b in mf.blocks), default=1) < 8
    for round_ in range(max_rounds):
        live_in, live_out = liveness(mf)
        g, moves, cost, across = build(mf, live_out)
        nodes = set()
        for b in mf.blocks:
            for mi in b.mis:
                for x in mi.d + mi.u:
                    if isinstance(x, int): nodes.add(x)
        alias = {}

        def find(x):
            while x in alias: x = alias[x]
            return x

        width = mf.width
        K = {c: len(r) for c, r in REGS.items()}

        def wdeg(n):
            return sum(width.get(m, 1) for m in g.adj[n]) + len(g.forbid[n])

        # Conservative (Briggs) coalescing of vreg-vreg moves.
        for a, b_, f in sorted(moves, key=lambda m: -m[2]):
            if not isinstance(b_, int): continue
            a, b_ = find(a), find(b_)
            if a == b_: continue
            if mf.cls.get(a) != mf.cls.get(b_): continue
            if width.get(a, 1) != width.get(b_, 1): continue
            if b_ in g.adj[a]: continue
            k = K[mf.cls[a]]
            neigh = g.adj[a] | g.adj[b_]
            if sum(1 for n in neigh if wdeg(n) >= k) + len(g.forbid[a] | g.forbid[b_]) >= k: continue
            # merge b_ into a
            alias[b_] = a
            for n in g.adj[b_]:
                g.adj[n].discard(b_); g.adj[n].add(a); g.adj[a].add(n)
            g.forbid[a] |= g.forbid[b_]
            del g.adj[b_]
            cost[a] += cost.get(b_, 0)
            if b_ in across: across.add(a)
            nodes.discard(b_)
        # Simplify / select.
        work = {find(n) for n in nodes}
        degree = {n: wdeg(n) for n in work}
        stack = []
        remaining = set(work)
        low = [n for n in remaining if degree[n] < K[mf.cls[n]]]
        while remaining:
            if low:
                # Simplify single registers first so pairs are coloured first.
                k = next((i for i in range(len(low)-1, -1, -1) if width.get(low[i], 1) == 1 and low[i] in remaining), None)
                n = low.pop(k if k is not None else -1)
                if n not in remaining: continue
            else:
                # Optimistic: push the cheapest spill candidate.
                n = min(remaining, key=lambda x: (float('inf') if x in no_spill else cost.get(x, 0.0)+1) / (degree[x]+1))
            remaining.discard(n)
            stack.append(n)
            for m in g.adj[n]:
                if m in remaining:
                    degree[m] -= width.get(n, 1)
                    if degree[m] < K[mf.cls[m]] and degree[m] + width.get(n, 1) >= K[mf.cls[m]]:
                        low.append(m)
        color = {}
        spilled = []
        partners = defaultdict(list)
        for a, b_, f in moves:
            fa = find(a)
            fb = find(b_) if isinstance(b_, int) else b_
            partners[fa].append(fb)
            if isinstance(fb, int): partners[fb].append(fa)
        used_callee = set()
        rot = defaultdict(int)
        while stack:
            n = stack.pop()
            c = mf.cls[n]
            regs = REGS[c]
            taken = set(g.forbid[n])
            for m in g.adj[n]:
                r = color.get(m)
                if r is None: continue
                wm = width.get(m, 1)
                if wm > 1:
                    i = regs.index(r); taken.update(regs[i:i+wm])
                else:
                    taken.add(r)
            w = width.get(n, 1)
            choice = None
            prefs = []
            for p in partners.get(n, ()):
                if isinstance(p, int) and width.get(p, 1) != width.get(n, 1): continue
                r = color.get(p) if isinstance(p, int) else p
                if r is not None and phys_class(r) == c: prefs.append(r)
            if w == 1:
                for r in prefs:
                    if r not in taken: choice = r; break
                if choice is None and mf.hot.get(n, 0) >= 8 and not mf.hot_saves_costly:
                    # Values in loops rotate through a pool of registers so that
                    # independent work does not serialise on reused registers.
                    # The pool is scratch registers plus whole quad groups, whose
                    # save/restore costs one transfer each.
                    pool = [r for r in ROTATION_POOL[c] if r not in taken]
                    if pool:
                        k = rot['hot'+c] % len(pool); rot['hot'+c] += 5
                        choice = pool[k]
                if choice is None:
                    scratch = [r for r in CALLER_SAVED_LIST[c] if r not in taken]
                    if scratch:
                        k = rot[c] % len(scratch); rot[c] += 1
                        choice = scratch[k]
                if choice is None:
                    for r in CALLEE_ORDER[c]:
                        if r not in taken and r in used_callee: choice = r; break
                if choice is None:
                    for r in CALLEE_ORDER[c]:
                        if r not in taken: choice = r; break
            else:
                # Register groups: w consecutive registers starting at a multiple of w.
                groups = []
                for i in range(0, len(regs)-w+1, w):
                    group = regs[i:i+w]
                    if int(group[-1][2:]) != int(group[0][2:]) + w - 1 or int(group[0][2:]) % w: continue
                    if not any(r in taken for r in group): groups.append(regs[i])
                for r in prefs:
                    if r in groups: choice = r; break
                if choice is None and groups:
                    if mf.hot.get(n, 0) >= 8 and not mf.hot_saves_costly:
                        k = rot['wide'] % min(len(groups), 3); rot['wide'] += 1
                        choice = groups[k]
                    else:
                        choice = groups[0]
            if choice is None:
                if n in no_spill:
                    victims = _pair_victims(n, g, color, width, regs, no_spill, cost)
                    if victims:
                        spilled.extend(victims); continue
                spilled.append(n); continue
            color[n] = choice
            if choice not in CALLER_SAVED:
                used_callee.add(choice)
                if w > 1: used_callee.update(regs[regs.index(choice):regs.index(choice)+w])
        if not spilled:
            _apply(mf, color, find)
            return
        _spill(mf, spilled, find, alias, no_spill)
    raise AssertionError(f'register allocation did not converge in {mf.fn.symbol}')


def _pair_victims(n, g, color, width, regs, no_spill, cost):
    """Cheapest set of coloured neighbours whose eviction frees an aligned group for n."""
    best = None
    w = width.get(n, 1)
    for i in range(0, len(regs)-w+1, w):
        pair = set(regs[i:i+w])
        if int(regs[i][2:]) % w: continue
        if pair & g.forbid[n]: continue
        victims = []
        for m in g.adj[n]:
            r = color.get(m)
            if r is None: continue
            wm = width.get(m, 1)
            occupied = set(regs[regs.index(r):regs.index(r)+wm])
            if occupied & pair: victims.append(m)
        if any(v in no_spill for v in victims): continue
        c = sum(cost.get(v, 0.0) + 1 for v in victims)
        if best is None or c < best[0]: best = (c, victims)
    return best[1] if best else None


# Operand positions that name a whole register pair (rendered Rn+1:n).
PAIR_POSITIONS = {('xmulw', 'd', 0), ('xlsh', 'd', 0), ('xlsh', 'u', 0)}


def _apply(mf, color, find):
    width = mf.width; part = mf.part

    def name(x, op, which, k):
        if not isinstance(x, int): return x
        root = find(x)
        r = color[root]
        if width.get(x, 1) == 2 and (op, which, k) not in PAIR_POSITIONS and part.get(x, 'lo') == 'hi':
            return f'XR{int(r[2:])+1}'
        return r

    used = set()
    for b in mf.blocks:
        out = []
        for mi in b.mis:
            op = mi.op
            mi.d = [name(x, op, 'd', k) for k, x in enumerate(mi.d)]
            mi.u = [name(x, op, 'u', k) for k, x in enumerate(mi.u)]
            if op == 'mov' and mi.d[0] == mi.u[0]:
                continue
            for k, x in enumerate(mi.d):
                if x in ALLOCATABLE:
                    used.add(x)
                    if (op, 'd', k) in PAIR_POSITIONS: used.add(f'XR{int(x[2:])+1}')
                    if op == 'ld' and mi.s == 'Q' and x.startswith('XR'):
                        used.update(f'XR{int(x[2:])+j}' for j in range(1, 4))
            out.append(mi)
        b.mis = out
    mf.used_regs = used


def _spill(mf, spilled, find, alias, no_spill):
    targets = set(spilled)
    members = defaultdict(set)
    for b in mf.blocks:
        for mi in b.mis:
            for x in mi.d + mi.u:
                if isinstance(x, int) and find(x) in targets: members[find(x)].add(x)
    # Rematerialisable: every def is a constant load.
    for root in spilled:
        vs = members[root] | {root}
        defs = [mi for b in mf.blocks for mi in b.mis if any(x in vs for x in mi.d)]
        remat = all(mi.op == 'li' for mi in defs) and len({(mi.imm, mi.s) for mi in defs}) == 1
        c = mf.cls[root]
        if remat:
            proto = defs[0]
            for b in mf.blocks:
                out = []
                for mi in b.mis:
                    if any(x in vs for x in mi.d) and mi.op == 'li':
                        continue
                    if any(x in vs for x in mi.u):
                        t = mf.vreg(c); no_spill.add(t)
                        out.append(MI('li', [t], [], proto.imm, proto.s))
                        mi.u = [t if x in vs else x for x in mi.u]
                        if mi.op == 'cset': pass
                    out.append(mi)
                b.mis = out
            continue
        size = mf.width.get(root, 1)
        obj = mf.fn.frame_object(size, 'spill')
        for b in mf.blocks:
            out = []
            for mi in b.mis:
                uses = any(x in vs for x in mi.u)
                defs_ = any(x in vs for x in mi.d)
                t = None
                if uses or defs_:
                    t = mf.vreg(c, size, mf.part.get(root, 'lo')); no_spill.add(t)
                if uses:
                    out.append(MI('ld', [t], ['J27'], 0, extra=('frame', obj.id)))
                    mi.u = [t if x in vs else x for x in mi.u]
                if defs_:
                    mi.d = [t if x in vs else x for x in mi.d]
                out.append(mi)
                if defs_:
                    out.append(MI('st', [], ['J27', t], 0, extra=('frame', obj.id)))
            b.mis = out
    # Clear coalescing aliases for the next round.
    alias.clear()
