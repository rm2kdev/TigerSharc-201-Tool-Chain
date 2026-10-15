"""Machine-independent SSA optimizations.

All passes keep SSA form. Memory is modelled conservatively: stores and calls
kill possibly-aliasing loads; volatile accesses are never removed, merged or
moved relative to other memory operations.
"""
from __future__ import annotations
from collections import defaultdict
from . import ir
from .ir import Ins, MASK, evaluate, compare, s32


# ---------------------------------------------------------------------------
# helpers

def def_map(fn):
    d = {}
    for b in fn.blocks:
        for p in b.phis: d[p.d] = p
        for i in b.ins:
            if i.d is not None: d[i.d] = i
    return d


def use_map(fn):
    u = defaultdict(list)
    for b in fn.blocks:
        for p in b.phis:
            for x in p.a: u[x].append(p)
        for i in b.ins:
            for x in i.a: u[x].append(i)
    return u


def has_side_effect(i):
    op = i.op
    if op in ('store', 'storeq', 'qcopy', 'call', 'halt', 'ret', 'jmp', 'br', 'jtab', 'setlc', 'lcbr'): return True
    if op == 'load': return i.vol
    return False


def is_pure(i):
    return i.op in ir.PURE or i.op in ('exactdiv',)


def replace_uses(fn, mapping):
    if not mapping: return
    def res(x):
        seen = 0
        while x in mapping and seen < 64:
            x = mapping[x]; seen += 1
        return x
    for b in fn.blocks:
        for p in b.phis: p.a = [res(x) for x in p.a]
        for i in b.ins: i.a = [res(x) for x in i.a]


# ---------------------------------------------------------------------------
# address / alias analysis

class Addr:
    __slots__ = ('kind', 'key', 'off', 'tag', 'base', 'boff', 'width')

    def __init__(self, kind, key, off, tag=None):
        self.kind = kind; self.key = key; self.off = off; self.tag = tag
        self.base = None; self.boff = None   # SSA base value and constant offset from it
        self.width = 1                       # words accessed


def address_of(defs, base, off, cache=None):
    a = _address_of(defs, base, off)
    # Record the peeled SSA base so equal (base, offset) pairs are one location.
    total = off; v = base
    for _ in range(16):
        i = defs.get(v)
        if i is None: break
        if i.op in ('add', 'sub') and len(i.a) == 2:
            c1 = defs.get(i.a[1])
            if c1 is not None and c1.op == 'const':
                total += s32(c1.imm) if i.op == 'add' else -s32(c1.imm); v = i.a[0]; continue
        if i.op == 'copy': v = i.a[0]; continue
        break
    a.base, a.boff = v, total
    return a


def _address_of(defs, base, off, cache=None):
    """Classify base+off as a known symbol/frame object or an opaque pointer."""
    total = off
    v = base
    for _ in range(16):
        i = defs.get(v)
        if i is None: break
        if i.op == 'sym': return Addr('sym', i.s, total + i.imm)
        if i.op == 'frame': return Addr('frame', i.imm, total)
        if i.op == 'add' and len(i.a) == 2:
            c0 = defs.get(i.a[0]); c1 = defs.get(i.a[1])
            if c1 is not None and c1.op == 'const': total += s32(c1.imm); v = i.a[0]; continue
            if c0 is not None and c0.op == 'const': total += s32(c0.imm); v = i.a[1]; continue
        if i.op == 'sub' and len(i.a) == 2:
            c1 = defs.get(i.a[1])
            if c1 is not None and c1.op == 'const': total -= s32(c1.imm); v = i.a[0]; continue
        if i.op == 'copy': v = i.a[0]; continue
        break
    # A symbol plus a variable index stays within that object in valid C.
    i = defs.get(v)
    if i is not None and i.op == 'add':
        for x in i.a:
            j = defs.get(x)
            if j is not None and j.op in ('sym', 'frame'):
                return Addr('symvar' if j.op == 'sym' else 'framevar', j.s if j.op == 'sym' else j.imm, None)
    return Addr('ptr', v, total)


class AliasInfo:
    def __init__(self, fn, defs, options=None):
        self.defs = defs
        options = options or {}
        # Frame objects whose address only feeds load/store bases never escape.
        self.private_frames = set()
        escapes = set()
        uses = use_map(fn)
        for b in fn.blocks:
            for i in b.ins:
                if i.op == 'frame':
                    obj = fn.frame[i.imm]
                    if self._escapes(i.d, uses, set()): escapes.add(i.imm)
        for f in fn.frame:
            if f.id not in escapes and f.kind != 'arg': self.private_frames.add(f.id)
        # Globals that no pointer can reach (whole-program knowledge, optional).
        self.unaliased = options.get('unaliased_globals') or frozenset()

    def _escapes(self, v, uses, seen):
        if v in seen: return False
        seen.add(v)
        for u in uses.get(v, ()):
            if u.op in ('load',) and u.a[0] == v: continue
            if u.op == 'store' and u.a[0] == v and u.a[1] != v: continue
            if u.op in ('add', 'sub') and u.a[0] == v:
                c = self.defs.get(u.a[1])
                if c is not None and c.op == 'const' and self._escapes(u.d, uses, seen) is False: continue
            if u.op == 'add' and len(u.a) == 2 and u.a[1] == v:
                c = self.defs.get(u.a[0])
                if c is not None and c.op == 'const' and self._escapes(u.d, uses, seen) is False: continue
            if u.op == 'copy' and not self._escapes(u.d, uses, seen): continue
            return True
        return False

    def addr(self, i):
        a = address_of(self.defs, i.a[0], i.imm)
        if i.op in ('loadq', 'storeq'): a.width = 4
        if isinstance(i.s, str) and i.s.startswith('restrict:'):
            a.tag = i.s   # 'restrict:<id>:src|dst' from a memcpy expansion
        return a

    def reachable_by_pointer(self, a):
        if a.kind in ('frame', 'framevar'): return a.key not in self.private_frames
        if a.kind in ('sym', 'symvar'): return a.key not in self.unaliased
        return True

    def may_alias(self, a, b, cross=False):
        """cross: a and b may belong to different iterations of the loop around them."""
        ta, tb = getattr(a, 'tag', None), getattr(b, 'tag', None)
        if ta and tb and ta.rsplit(':', 1)[0] == tb.rsplit(':', 1)[0] and ta != tb                 and not (cross and (ta.startswith('restrict:s') or '.i' in ta)):
            # Source and destination of one memcpy never overlap. A straight-line
            # expansion (restrict:s) in a loop is a new call every iteration, and so
            # is an inlined body (.i): restrict holds within one call only.
            return False
        wa, wb = getattr(a, 'width', 1), getattr(b, 'width', 1)

        def overlap(x, y):
            return x is None or y is None or (x < y + wb and y < x + wa)
        if a.kind in ('sym', 'frame') and b.kind == a.kind:
            return a.key == b.key and overlap(a.off, b.off)
        if a.kind in ('sym', 'symvar') and b.kind in ('sym', 'symvar'):
            return a.key == b.key
        if a.kind in ('frame', 'framevar') and b.kind in ('frame', 'framevar'):
            return a.key == b.key
        if a.kind == 'ptr' and b.kind == 'ptr':
            if a.key == b.key: return overlap(a.off, b.off)
            return True
        if a.kind == 'ptr': return self.reachable_by_pointer(b)
        if b.kind == 'ptr': return self.reachable_by_pointer(a)
        return False   # distinct known objects

    def call_clobbers(self, a):
        """Can a call modify/read this location?"""
        if a.kind in ('frame', 'framevar'): return a.key not in self.private_frames
        return True


# ---------------------------------------------------------------------------
# SCCP

TOP = object()
BOT = object()


def sccp(fn):
    ir.link(fn)
    defs = def_map(fn)
    users = use_map(fn)
    val = {}
    exec_blocks = set()
    exec_edges = set()
    flow = [fn.entry]
    ssa = []

    def get(v):
        return val.get(v, TOP)

    def setv(v, x):
        old = val.get(v, TOP)
        if old is BOT or old is x or (old is not TOP and x is not BOT and x is not TOP and old == x): return
        if old is not TOP and x is not BOT and x is not TOP and old != x: x = BOT
        val[v] = x
        ssa.extend(users.get(v, ()))

    def mark_edge(a, b):
        if (a.id, b.id) in exec_edges: return
        exec_edges.add((a.id, b.id))
        if b.id not in exec_blocks:
            flow.append(b)
        else:
            for p in b.phis: visit(p)

    def visit(i):
        b = i.blk
        if b.id not in exec_blocks: return
        op = i.op
        if op == 'phi':
            r = TOP
            for pred, x in zip(i.t, i.a):
                if (pred.id, b.id) not in exec_edges: continue
                v = get(x)
                if v is TOP: continue
                if v is BOT: r = BOT; break
                if r is TOP: r = v
                elif r != v: r = BOT; break
            setv(i.d, r); return
        if op == 'const': setv(i.d, i.imm & MASK); return
        if op == 'jmp': mark_edge(b, i.t[0]); return
        if op == 'br':
            x, y = get(i.a[0]), get(i.a[1])
            if x is TOP or y is TOP: return
            if x is BOT or y is BOT:
                mark_edge(b, i.t[0]); mark_edge(b, i.t[1]); return
            mark_edge(b, i.t[0] if compare(i.s, x, y) else i.t[1]); return
        if op == 'jtab':
            x = get(i.a[0])
            if x is TOP: return
            if x is BOT or x >= len(i.t):
                for t in i.t: mark_edge(b, t)
            else: mark_edge(b, i.t[x])
            return
        if i.d is None: return
        if op == 'copy': setv(i.d, get(i.a[0])); return
        if op in ir.BINARY or op in ir.UNARY or op in ('cmp', 'exactdiv', 'sel'):
            vals = [get(x) for x in i.a]
            if any(v is BOT for v in vals):
                # x*0, x&0 are known even when x is not.
                if op in ('mul', 'and', 'mulhu', 'mulhs', 'mulq16', 'mulfr') and any(v == 0 and v is not BOT and v is not TOP for v in vals):
                    setv(i.d, 0); return
                setv(i.d, BOT); return
            if any(v is TOP for v in vals): return
            if op == 'cmp': setv(i.d, int(compare(i.s, vals[0], vals[1]))); return
            if op == 'exactdiv':
                r = evaluate('div', vals[0], i.imm)
            elif op == 'sel':
                r = vals[2] if compare(i.s, vals[0], vals[1]) else vals[3]
            else:
                r = evaluate(op, *vals)
            setv(i.d, BOT if r is None else r); return
        setv(i.d, BOT)

    while flow or ssa:
        while flow:
            b = flow.pop()
            if b.id in exec_blocks: continue
            exec_blocks.add(b.id)
            for p in b.phis: visit(p)
            for i in b.ins: visit(i)
        while ssa:
            visit(ssa.pop())
    # Rewrite.
    changed = False
    for b in fn.blocks:
        if b.id not in exec_blocks: continue
        for p in list(b.phis):
            v = val.get(p.d, TOP)
            if v is not BOT and v is not TOP:
                b.phis.remove(p)
                b.ins.insert(0, Ins('const', p.d, (), v)); changed = True
        for k, i in enumerate(b.ins):
            if i.d is not None and i.op != 'const' and not has_side_effect(i) and i.op != 'call':
                v = val.get(i.d, TOP)
                if v is not BOT and v is not TOP:
                    b.ins[k] = Ins('const', i.d, (), v); changed = True
        t = b.term
        if t.op in ('br', 'jtab'):
            live = [s for s in t.t if (b.id, s.id) in exec_edges]
            if len(set(id(s) for s in live)) == 1:
                b.ins[-1] = Ins('jmp', t=[live[0]]); changed = True
    before = len(fn.blocks)
    ir.link(fn)
    return changed or len(fn.blocks) != before


# ---------------------------------------------------------------------------
# peephole simplification + copy propagation

def is_pow2(v):
    return v > 0 and v & (v-1) == 0


def instcombine(fn):
    defs = def_map(fn)
    changed = False
    subst = {}
    newconst = []

    def c(v):
        i = defs.get(v)
        return i.imm if i is not None and i.op == 'const' else None

    def mkconst(b, pos, value):
        n = fn.vreg()
        i = Ins('const', n, (), value & MASK); i.blk = b
        b.ins.insert(pos, i); defs[n] = i
        return n

    nuses = defaultdict(int)
    for b in fn.blocks:
        for i in b.phis + b.ins:
            for x in i.a: nuses[x] += 1

    for b in fn.blocks:
        k = 0
        while k < len(b.ins):
            i = b.ins[k]
            op = i.op
            if op == 'copy':
                subst[i.d] = i.a[0]; b.ins.pop(k); changed = True; continue
            if i.a and any(x in subst for x in i.a):
                i.a = [_res(subst, x) for x in i.a]
            if op in ir.COMMUTATIVE and len(i.a) == 2 and c(i.a[0]) is not None and c(i.a[1]) is None:
                i.a = [i.a[1], i.a[0]]
            a = i.a
            ca = c(a[0]) if a else None
            cb = c(a[1]) if len(a) > 1 else None
            r = None   # replacement vreg
            if a and ca is not None and (op in ir.UNARY and len(a) == 1 or op in ir.BINARY and cb is not None):
                v = evaluate(op, ca, cb or 0)
                if v is not None:
                    i.op = 'const'; i.a = []; i.imm = v & MASK; changed = True
                    k += 1; continue
            if op == 'cmp' and ca is not None and cb is not None:
                i.op = 'const'; i.imm = int(compare(i.s, ca, cb)); i.a = []; i.s = None; changed = True
                k += 1; continue
            if op == 'sel' and ca is not None and cb is not None:
                r = a[2] if compare(i.s, ca, cb) else a[3]
                subst[i.d] = r; b.ins.pop(k); changed = True; continue
            if op == 'add':
                if cb == 0: r = a[0]
                else:
                    x = defs.get(a[0])
                    inner = defs.get(x.a[0]) if x is not None and x.a else None
                    # Folding (p + c1) + c2 into p + (c1+c2) would keep p alive past
                    # x's block; skip it when p is a phi from another block.
                    far_phi = inner is not None and inner.op == 'phi' and x.blk is not b
                    if cb is not None and x is not None and x.op in ('add', 'sub') and c(x.a[1]) is not None and not far_phi:
                        k2 = c(x.a[1]) if x.op == 'add' else -c(x.a[1])
                        i.a = [x.a[0], mkconst(b, k, cb + k2)]; k += 1; changed = True; continue
                    y = defs.get(a[1])
                    if y is not None and y.op == 'neg':
                        i.op = 'sub'; i.a = [a[0], y.a[0]]; changed = True; continue
                    # Reassociate constants outwards: x + (y + c) -> (x + y) + c, so
                    # offsets collect at the top (and fold into memory offsets).
                    if cb is None and 'reassoc' not in DISABLED:
                        for k1 in (1, 0):
                            t_ = defs.get(a[k1])
                            if t_ is None or t_.op != 'add' or c(t_.a[1]) is None or t_.blk is not b: continue
                            if nuses.get(a[k1], 0) > 1: continue
                            if defs.get(t_.a[0]) is not None and defs[t_.a[0]].op == 'const': continue
                            other = a[1-k1]
                            n_ = Ins('add', fn.vreg(), [other, t_.a[0]]); n_.blk = b
                            b.ins.insert(k, n_); defs[n_.d] = n_; k += 1
                            i.a = [n_.d, t_.a[1]]
                            changed = True
                            break
                        else:
                            k += 1
                            continue
                        continue
            elif op == 'sub':
                if cb == 0: r = a[0]
                elif a[0] == a[1]: i.op = 'const'; i.a = []; i.imm = 0; changed = True
                elif cb is not None:
                    i.op = 'add'; i.a = [a[0], mkconst(b, k, -cb)]; k += 1; changed = True; continue
                elif ca == 0:
                    i.op = 'neg'; i.a = [a[1]]; changed = True
            elif op == 'mul':
                if cb == 1: r = a[0]
                elif cb == 0: i.op = 'const'; i.a = []; i.imm = 0; changed = True
                elif cb is not None and is_pow2(cb):
                    i.op = 'shl'; i.a = [a[0], mkconst(b, k, cb.bit_length()-1)]; k += 1; changed = True; continue
                elif cb == MASK:
                    i.op = 'neg'; i.a = [a[0]]; changed = True
                elif cb is not None and _distribute(fn, b, k, i, defs.get(a[0]), cb, c, nuses, defs, mkconst, lambda v: (v * cb) & MASK):
                    changed = True; continue
            elif op in ('and',):
                if cb == MASK: r = a[0]
                elif cb == 0: i.op = 'const'; i.a = []; i.imm = 0; changed = True
                elif a[0] == a[1]: r = a[0]
            elif op in ('or', 'xor'):
                if cb == 0: r = a[0]
                elif op == 'or' and a[0] == a[1]: r = a[0]
                elif op == 'xor' and a[0] == a[1]: i.op = 'const'; i.a = []; i.imm = 0; changed = True
                elif op == 'xor' and cb == MASK: i.op = 'not'; i.a = [a[0]]; changed = True
            elif op in ('shl', 'shr', 'sar'):
                if cb == 0: r = a[0]
                elif cb is not None and cb > 31:
                    pass
                elif ca == 0: i.op = 'const'; i.a = []; i.imm = 0; changed = True
                elif cb is not None:
                    x = defs.get(a[0])
                    # (x << k1) << k2, (x >> k1) >> k2
                    if x is not None and x.op == op and c(x.a[1]) is not None and c(x.a[1]) + cb <= 31:
                        i.a = [x.a[0], mkconst(b, k, c(x.a[1]) + cb)]; k += 1; changed = True; continue
                    # (x + c) << k -> (x << k) + (c << k)
                    if op == 'shl' and _distribute(fn, b, k, i, x, cb, c, nuses, defs, mkconst, lambda v: (v << cb) & MASK):
                        changed = True; continue
            elif op in ('udiv', 'div'):
                if cb == 1: r = a[0]
            elif op in ('umod', 'mod'):
                if cb == 1: i.op = 'const'; i.a = []; i.imm = 0; changed = True
            elif op in ('load', 'store', 'loadq', 'storeq') and 'reassoc' not in DISABLED:
                # [p + c] + imm -> [p] + (imm + c)
                x = defs.get(a[0])
                if x is not None and x.op == 'add' and c(x.a[1]) is not None and defs.get(x.a[0]) is not None \
                        and defs[x.a[0]].op not in ('const',) and (op in ('load', 'store') or s32(c(x.a[1])) % 4 == 0):
                    p_ = defs[x.a[0]]
                    # p' = p + c feeding p's phi: addressing through p would keep
                    # both alive (and p live out of its loop).
                    if not (p_.op == 'phi' and (p_.blk is b or x.d in p_.a)):
                        i.a[0] = x.a[0]; i.imm = s32(i.imm + c(x.a[1])); changed = True
                        continue
            elif op == 'neg':
                x = defs.get(a[0])
                if x is not None and x.op == 'neg': r = x.a[0]
            elif op == 'not':
                x = defs.get(a[0])
                if x is not None and x.op == 'not': r = x.a[0]
            elif op == 'sel':
                # sel(c, v OP m, v) -> v OP sel(c, m, 0): the select no longer waits for v.
                x_, y_, vt, vf = a
                t_ = defs.get(vt)
                if t_ is not None and t_.op in ('or', 'add', 'xor') and vf in t_.a and len(t_.a) == 2:
                    m_ = t_.a[1] if t_.a[0] == vf else t_.a[0]
                    z = mkconst(b, k, 0); k += 1
                    s2 = Ins('sel', fn.vreg(), [x_, y_, m_, z], 0, i.s); s2.blk = b
                    b.ins.insert(k, s2); defs[s2.d] = s2; k += 1
                    i.op = t_.op; i.a = [vf, s2.d]; i.s = None
                    changed = True
            elif op == 'cmp':
                if ca is not None and cb is None:
                    i.a = [a[1], a[0]]; i.s = ir.SWAP[i.s]; changed = True
                elif a[0] == a[1]:
                    i.op = 'const'; i.a = []; i.imm = int(i.s in ('eq', 'le', 'ge', 'leu', 'geu')); changed = True
                else:
                    # cmp(cmp(..), 0) with eq/ne folds into the inner comparison.
                    x = defs.get(a[0])
                    if cb == 0 and x is not None and x.op == 'cmp' and i.s in ('eq', 'ne'):
                        if i.s == 'ne': r = a[0]
                        else:
                            i.s = ir.NEGATE[x.s]; i.a = list(x.a); changed = True
            if r is not None:
                subst[i.d] = r; b.ins.pop(k); changed = True; continue
            k += 1
        t = b.term
        if t is not None and t.a:
            t.a = [_res(subst, x) for x in t.a]
    # Branch simplification: br ne (cmp cc x y), 0 -> br cc x y
    folded = False
    for b in fn.blocks:
        for p in b.phis: p.a = [_res(subst, x) for x in p.a]
        for i in b.ins:
            if any(x in subst for x in i.a): i.a = [_res(subst, x) for x in i.a]
        t = b.term
        if t.op == 'br':
            x, y = t.a
            cy = c(y); cx = c(x)
            if cx is not None and cy is not None:
                n = Ins('jmp', t=[t.t[0] if compare(t.s, cx, cy) else t.t[1]]); n.blk = b
                b.ins[-1] = n; changed = folded = True
                continue
            if cx is not None and cy is None:
                t.a = [y, x]; t.s = ir.SWAP[t.s]; x, y = y, x; cy = cx; changed = True
            d = defs.get(x)
            if cy == 0 and d is not None and d.op == 'cmp' and t.s in ('eq', 'ne'):
                t.a = list(d.a); t.s = d.s if t.s == 'ne' else ir.NEGATE[d.s]; changed = True
    if folded: ir.link(fn)
    return changed


def _distribute(fn, b, k, i, x, cb, c, nuses, defs, mkconst, scale):
    """i = (x.a[0] + c1) OP cb with x a single-use add: rewrite in place as
    (x.a[0] OP cb) + scale(c1). The new product/shift is inserted before i."""
    if 'reassoc' in DISABLED: return False
    if x is None or x.op != 'add' or c(x.a[1]) is None or nuses.get(x.d, 0) != 1 or x.blk is not b: return False
    t = Ins(i.op, fn.vreg(), [x.a[0], i.a[1]]); t.blk = b
    b.ins.insert(k, t); defs[t.d] = t
    off = mkconst(b, k+1, scale(c(x.a[1]) & MASK))
    i.op = 'add'; i.a = [t.d, off]
    nuses[t.d] = 1
    return True


def _res(subst, x):
    n = 0
    while x in subst and n < 100:
        x = subst[x]; n += 1
    return x


# ---------------------------------------------------------------------------
# global value numbering (dominator scoped) + local memory forwarding

def gvn(fn, alias):
    order = ir.dominators(fn)
    defs = def_map(fn)
    subst = {}
    changed = False
    table = {}
    const_of = {}

    def key(i):
        a = [_res(subst, x) for x in i.a]
        if i.op in ir.COMMUTATIVE and len(a) == 2 and a[0] > a[1]: a = [a[1], a[0]]
        if i.op == 'cmp' and a[0] > a[1]:
            return ('cmp', a[1], a[0], ir.SWAP[i.s])
        return (i.op, tuple(a), i.imm, i.s if not isinstance(i.s, list) else None)

    work = [(fn.entry, None, [])]
    while work:
        b, undo, inherited = work.pop()
        if undo is not None:
            for k in undo:
                del table[k]
            continue
        added = []
        for p in b.phis:
            p.a = [_res(subst, x) for x in p.a]
            vals = set(p.a) - {p.d}
            if len(vals) == 1:
                subst[p.d] = next(iter(vals))
        if _merge_phis(b, subst): changed = True
        b.phis = [p for p in b.phis if p.d not in subst]
        # Memory state: (Addr, value) list, inherited along extended basic blocks
        # (a block whose only predecessor is its parent); cleared by calls.
        avail = list(inherited)
        kept = []
        for i in b.ins:
            if i.a: i.a = [_res(subst, x) for x in i.a]
            op = i.op
            if op == 'load' and not i.vol:
                ad = alias.addr(i)
                hit = None
                for (a2, v2, isload) in avail:
                    if _same_location(a2, ad):
                        hit = v2; break
                if hit is not None:
                    subst[i.d] = hit; changed = True; continue
                avail.append((ad, i.d, True))
            elif op == 'store':
                ad = alias.addr(i)
                if i.vol:
                    avail = [e for e in avail if not alias.may_alias(e[0], ad)]
                else:
                    avail = [e for e in avail if not alias.may_alias(e[0], ad)]
                    avail.append((ad, i.a[1], False))
            elif op == 'call':
                avail = [e for e in avail if not alias.call_clobbers(e[0])]
            elif op == 'storeq':
                avail = [e for e in avail if not alias.may_alias(e[0], alias.addr(i))]
            elif op == 'qcopy':
                avail = []   # writes an unbounded range through its first operand
            elif op == 'load' and i.vol:
                pass
            elif is_pure(i) and i.d is not None and op not in ('copy',) and not (op in ir.TRAPPING):
                k = key(i)
                prev = table.get(k)
                if prev is not None:
                    subst[i.d] = prev; changed = True; continue
                table[k] = i.d; added.append(k)
            elif op in ir.TRAPPING and i.d is not None:
                k = key(i)
                prev = table.get(k)
                if prev is not None:
                    subst[i.d] = prev; changed = True; continue
                table[k] = i.d; added.append(k)
            kept.append(i)
        b.ins = kept
        work.append((b, added, None))
        for c in reversed(b.dom_children):
            work.append((c, None, avail if len(c.preds) == 1 and c.preds[0] is b else []))
    replace_uses(fn, subst)
    # Loop phis whose back-edge values only became equal during the walk:
    # merge them and number again (their loads and stores now share a base).
    subst = {}
    for b in fn.blocks:
        if _merge_phis(b, subst): b.phis = [p for p in b.phis if p.d not in subst]
    if subst:
        replace_uses(fn, subst)
        alias.defs = def_map(fn)
        gvn(fn, alias)
        changed = True
    return changed


def _merge_phis(b, subst):
    """Phis of one block merging the same values are one value."""
    seen = {}
    found = False
    for p in b.phis:
        if p.d in subst: continue
        k = tuple(sorted((t.id, _res(subst, v)) for t, v in zip(p.t, p.a)))
        if k in seen:
            subst[p.d] = seen[k]; found = True
        else:
            seen[k] = p.d
    return found


def _same_location(a, b):
    if getattr(a, 'width', 1) != 1 or getattr(b, 'width', 1) != 1: return False
    if a.base is not None and a.base == b.base and a.boff == b.boff: return True
    if a.kind != b.kind: return False
    if a.kind in ('symvar', 'framevar'): return False
    return a.key == b.key and a.off == b.off


# ---------------------------------------------------------------------------
# dead code elimination

def dce(fn):
    live = set()
    defs = def_map(fn)
    work = []
    for b in fn.blocks:
        for i in b.ins:
            if has_side_effect(i):
                for x in i.a:
                    if x not in live: live.add(x); work.append(x)
    while work:
        v = work.pop()
        i = defs.get(v)
        if i is None: continue
        for x in i.a:
            if x not in live: live.add(x); work.append(x)
    changed = False
    for b in fn.blocks:
        n = len(b.ins) + len(b.phis)
        b.phis = [p for p in b.phis if p.d in live]
        b.ins = [i for i in b.ins if i.d is None or i.d in live or has_side_effect(i)]
        if len(b.ins) + len(b.phis) != n: changed = True
    return changed


def _nonzero_divisor(defs, i):
    d = defs.get(i.a[1])
    return d is not None and d.op == 'const' and d.imm & MASK != 0


# ---------------------------------------------------------------------------
# CFG simplification

def simplify_cfg(fn):
    changed = True
    any_change = False
    while changed:
        changed = False
        ir.link(fn)
        for b in list(fn.blocks):
            t = b.term
            # Merge a block into its unique predecessor.
            if len(b.preds) == 1 and b is not fn.entry:
                p = b.preds[0]
                if len(p.succs) == 1 and p.term.op == 'jmp' and p is not b:
                    for ph in b.phis:
                        ph_i = Ins('copy', ph.d, [ph.a[0]]); ph_i.blk = p
                        p.ins.insert(len(p.ins)-1, ph_i)
                    b.phis = []
                    p.ins.pop()
                    for i in b.ins: i.blk = p
                    p.ins.extend(b.ins)
                    b.ins = [Ins('jmp', t=[b])]  # detached; link() drops it
                    fn.blocks.remove(b)
                    # Successor phis referring to b now come from p.
                    for s in p.term.t:
                        for ph in s.phis:
                            ph.t = [p if x is b else x for x in ph.t]
                    changed = True
                    break
            # Thread jumps through empty blocks.
            if t.op == 'jmp' and len(b.ins) == 1 and not b.phis and b is not fn.entry:
                target = t.t[0]
                if target is b: continue
                ok = True
                for p in b.preds:
                    if p in target.preds and target.phis: ok = False
                if not ok: continue
                for p in list(b.preds):
                    pt = p.term
                    pt.t = [target if x is b else x for x in pt.t]
                    for ph in target.phis:
                        k = ph.t.index(b)
                        ph.t.append(p); ph.a.append(ph.a[k])
                for ph in target.phis:
                    k = ph.t.index(b)
                    del ph.t[k]; del ph.a[k]
                fn.blocks.remove(b)
                changed = True
                break
        any_change |= changed
    ir.link(fn)
    return any_change


def fuse_range_checks(fn):
    """`x < 0 || x >= C` (and `x >= 0 && x < C`, either order) as two
    branches to one exit becomes a single unsigned `x >= C` branch.

    Block A ends in a branch on x; its successor B holds only constants and
    a branch on x, has A as its only predecessor and shares A's other
    successor T (with equal phi values from A and B). When one of the two
    conditions leading to T is `x < 0` and the other `x >= C` (C > 0),
    A branches on `x >=u C` to T and otherwise to B's other successor."""
    ir.link(fn)
    defs = def_map(fn)

    def const(v):
        d = defs.get(v)
        return s32(d.imm) if d is not None and d.op == 'const' else None

    def exit_test(br, t):
        """('neg', x) for x < 0 or ('ge', x, C) for x >= C: the condition taking br to t."""
        if br.op != 'br' or br.s not in ('lt', 'le', 'gt', 'ge') or t not in br.t: return None
        x, k = br.a[0], const(br.a[1])
        cc = br.s
        if k is None:
            k = const(br.a[0])
            if k is None: return None
            x = br.a[1]; cc = ir.SWAP[cc]
        if br.t[1] is t: cc = ir.NEGATE[cc]
        if (cc, k) in (('lt', 0), ('le', -1)): return ('neg', x)
        if cc == 'ge' and k > 0: return ('ge', x, k)
        if cc == 'gt' and 0 <= k < 0x7fffffff: return ('ge', x, k + 1)
        return None

    changed = again = True
    while again:
        again = False
        for a in fn.blocks:
            if _fuse_range_pair(fn, a, exit_test):
                changed = again = True
                ir.link(fn)
                break
    return changed


def _fuse_range_pair(fn, a, exit_test):
    ta = a.term
    if ta.op != 'br' or ta.t[0] is ta.t[1]: return False
    for b in ta.t:
        if b is a or len(b.preds) != 1 or b.phis: continue
        tb = b.term
        if tb.op != 'br' or any(i.op != 'const' for i in b.ins[:-1]): continue
        t = ta.t[1] if ta.t[0] is b else ta.t[0]
        if t not in tb.t or t is b: continue
        f = tb.t[1] if tb.t[0] is t else tb.t[0]
        if f is t: continue
        ea, eb = exit_test(ta, t), exit_test(tb, t)
        if ea is None or eb is None or ea[1] != eb[1] or {ea[0], eb[0]} != {'neg', 'ge'}: continue
        if any(p.a[p.t.index(a)] != p.a[p.t.index(b)] for p in t.phis): continue
        if a in f.preds: continue
        c = ea[2] if ea[0] == 'ge' else eb[2]
        k = Ins('const', fn.vreg(), (), c & MASK)
        # B's constants move up: A dominates everything B did.
        a.ins[-1:] = b.ins[:-1] + [k, Ins('br', None, [ea[1], k.d], 0, 'geu', [t, f])]
        b.ins[:-1] = []
        for p in t.phis:
            j = p.t.index(b); del p.t[j]; del p.a[j]
        for p in f.phis:
            p.t = [a if x is b else x for x in p.t]
        return True
    return False


def thread_phi_branches(fn):
    """A block that only branches on its own phis (e.g. `if (j == 8)` after an
    unrolled search loop): a predecessor whose incoming values decide the
    branch jumps straight to the chosen successor."""
    ir.link(fn)
    defs = def_map(fn)
    uses = use_map(fn)
    changed = False
    for b in list(fn.blocks):
        t = b.term
        if t is None or t.op != 'br' or len(b.ins) != 1 or not b.phis or b is fn.entry: continue
        phis = {p.d: p for p in b.phis}
        if any(u.blk is not b or u is not t for p in b.phis for u in uses.get(p.d, ())): continue
        order = list(b.phis[0].t)
        if len(set(order)) != len(order): continue
        for pred in order:
            idx = b.phis[0].t.index(pred)
            vals = []
            for x in t.a:
                v = phis[x].a[phis[x].t.index(pred)] if x in phis else x
                c = defs.get(v)
                vals.append(c.imm & MASK if c is not None and c.op == 'const' else None)
            if None in vals: continue
            target = t.t[0] if ir.compare(t.s, vals[0], vals[1]) else t.t[1]
            pt = pred.term
            if target is b or target in pred.succs or pt.t.count(b) != 1: continue
            pt.t = [target if x is b else x for x in pt.t]
            for ph in target.phis:
                v = ph.a[ph.t.index(b)]
                if v in phis: v = phis[v].a[phis[v].t.index(pred)]
                ph.t.append(pred); ph.a.append(v)
            for ph in b.phis:
                k = ph.t.index(pred)
                del ph.t[k]; del ph.a[k]
            ir.link(fn)
            changed = True
    if changed: simplify_cfg(fn)
    return changed


# ---------------------------------------------------------------------------
# loop invariant code motion

def ensure_preheader(fn, lp):
    outside = [p for p in lp.header.preds if p.id not in lp.blocks]
    if len(outside) == 1 and len(outside[0].succs) == 1:
        lp.preheader = outside[0]
        return False
    pre = fn.block()
    pre.ins.append(Ins('jmp', t=[lp.header]))
    pre.ins[0].blk = pre
    for p in outside:
        t = p.term
        t.t = [pre if x is lp.header else x for x in t.t]
    # Split header phis: outside operands merge in the preheader.
    for ph in lp.header.phis:
        outside_vals = [(x, v) for x, v in zip(ph.t, ph.a) if x.id not in lp.blocks]
        inside = [(x, v) for x, v in zip(ph.t, ph.a) if x.id in lp.blocks]
        if len(outside_vals) == 1:
            nv = outside_vals[0][1]
        else:
            nv = fn.vreg()
            np_ = Ins('phi', nv, [v for _, v in outside_vals]); np_.t = [x for x, _ in outside_vals]; np_.blk = pre
            pre.phis.append(np_)
        ph.t = [pre] + [x for x, _ in inside]; ph.a = [nv] + [v for _, v in inside]
    lp.preheader = pre
    return True


def licm(fn, alias):
    ir.link(fn)
    lps = ir.loops(fn)
    if not lps: return False
    changed = False
    for lp in lps:
        if ensure_preheader(fn, lp): changed = True
    if changed:
        ir.link(fn)
        lps = ir.loops(fn)
        for lp in lps: ensure_preheader(fn, lp)
    defs = def_map(fn)
    ids = {b.id: b for b in fn.blocks}
    moved = False
    # Innermost first so hoisted code can move further out.
    for lp in sorted(lps, key=lambda l: -l.depth):
        pre = lp.preheader
        blocks = [ids[x] for x in lp.blocks if x in ids]
        blocks.sort(key=lambda b: b.order)
        inside = set()
        has_call = False
        stores = []
        for b in blocks:
            for p in b.phis: inside.add(p.d)
            for i in b.ins:
                if i.d is not None: inside.add(i.d)
                if i.op in ('call', 'storeq', 'qcopy'): has_call = True
                elif i.op == 'store' or (i.op == 'load' and i.vol): stores.append(i)
        exits_src = [b for b in blocks if any(s.id not in lp.blocks for s in b.succs)]
        progress = True
        while progress:
            progress = False
            for b in blocks:
                keep = []
                for i in b.ins:
                    if i.d is not None and i.d in inside and all(x not in inside for x in i.a) and _hoistable(i, defs, alias, has_call, stores, b, exits_src, lp.latches):
                        i.blk = pre
                        pre.ins.insert(len(pre.ins)-1, i)
                        inside.discard(i.d)
                        progress = moved = True
                    else:
                        keep.append(i)
                b.ins = keep
        if 'areassoc' not in DISABLED and _reassociate_addresses(fn, lp, blocks, inside, defs):
            moved = True
    return moved or changed


def _reassociate_addresses(fn, lp, blocks, inside, defs):
    """[inv + var + k] -> [(inv + k) + var]: the invariant part is formed in
    the preheader so the access can use [Jm + Jn] indexed addressing."""
    pre = lp.preheader
    done = False
    nuses = defaultdict(int)
    for b in fn.blocks:
        for i in b.phis + b.ins:
            for x in i.a: nuses[x] += 1
    budget = 4   # each rewrite keeps one more invariant register live in the loop
    for b in blocks:
        out = []
        for i in b.ins:
            if i.op in ('load', 'store') and i.imm and not i.vol:
                base = defs.get(i.a[0])
                if budget and base is not None and base.op == 'add' and len(base.a) == 2 and base.d in inside and nuses[base.d] == 1:
                    x, y = base.a
                    inv, var = (x, y) if x not in inside and y in inside else (y, x) if y not in inside and x in inside else (None, None)
                    if inv is not None:
                        c = Ins('const', fn.vreg(), (), i.imm & MASK); c.blk = pre
                        t = Ins('add', fn.vreg(), [inv, c.d]); t.blk = pre
                        pre.ins[len(pre.ins)-1:len(pre.ins)-1] = [c, t]
                        n = Ins('add', fn.vreg(), [t.d, var]); n.blk = b
                        defs[c.d] = c; defs[t.d] = t; defs[n.d] = n; inside.add(n.d)
                        out.append(n)
                        i.a = [n.d] + list(i.a[1:]); i.imm = 0
                        done = True; budget -= 1
            out.append(i)
        b.ins = out
    return done


def _hoistable(i, defs, alias, has_call, stores, b, exits_src, latches):
    op = i.op
    if op in ('const', 'sym', 'frame'): return True
    if op in ir.TRAPPING:
        return _nonzero_divisor(defs, i)
    if is_pure(i) and op != 'copy': return True
    if op == 'load' and not i.vol:
        ad = alias.addr(i)
        if has_call and alias.call_clobbers(ad): return False
        for s in stores:
            if s.op == 'load': return False   # volatile ordering
            if alias.may_alias(alias.addr(s), ad, cross=True): return False
        if ad.kind in ('sym', 'frame'): return True
        # Pointer loads must execute on every iteration before any exit.
        return all(ir.dominates(b, e) for e in exits_src) and all(ir.dominates(b, l) for l in latches)
    return False


# ---------------------------------------------------------------------------
# legalization of operations with constant operands

def magic_unsigned(d):
    """(multiplier, add, shift) for n/d with uint32 n (Granlund-Montgomery)."""
    k = d.bit_length() - 1
    m, r = divmod(1 << (32 + k), d)
    add = d - r >= 1 << k
    if add: m = 2*m + (2*r >= d)
    m = (m + 1) & MASK
    return m, add, k


def magic_signed(d):
    """Hacker's Delight 10-1: (M, s) for signed division by d (|d| >= 2)."""
    ad = abs(d)
    t = 0x80000000 + ((d & MASK) >> 31)
    anc = t - 1 - t % ad
    p = 31
    q1, r1 = divmod(0x80000000, anc)
    q2, r2 = divmod(0x80000000, ad)
    while True:
        p += 1
        q1 *= 2; r1 *= 2
        if r1 >= anc: q1 += 1; r1 -= anc
        q2 *= 2; r2 *= 2
        if r2 >= ad: q2 += 1; r2 -= ad
        delta = ad - r2
        if not (q1 < delta or (q1 == delta and r1 == 0)): break
    M = q2 + 1
    if d < 0: M = -M
    return M & MASK, p - 32


def legalize(fn):
    """Replace constant division/multiplication by cheaper sequences."""
    defs = def_map(fn)
    changed = False

    def c(v):
        i = defs.get(v)
        return i.imm if i is not None and i.op == 'const' else None

    for b in fn.blocks:
        out = []
        for i in b.ins:
            new = _legalize_one(fn, i, c)
            if new is None:
                out.append(i); continue
            for n in new:
                n.blk = b
                if n.d is not None: defs[n.d] = n
            out.extend(new); changed = True
        b.ins = out
    return changed


def _legalize_one(fn, i, c):
    op = i.op
    seq = []

    def emit(o, a, imm=0, d=None):
        n = Ins(o, fn.vreg() if d is None else d, a, imm)
        seq.append(n)
        return n.d

    def const(v):
        return emit('const', [], v & MASK)

    if op == 'exactdiv':
        size = i.imm
        k = (size & -size).bit_length() - 1
        m = size >> k
        x = i.a[0]
        if k: x = emit('sar', [x, const(k)])
        if m != 1:
            inv = pow(m, -1, 1 << 32)
            x = emit('mul', [x, const(inv)])
        emit('copy', [x], d=i.d)
        return seq
    if op in ('udiv', 'umod'):
        d = c(i.a[1])
        if d is None or d == 0 or d == 1: return None
        n = i.a[0]
        if is_pow2(d):
            k = d.bit_length() - 1
            if op == 'udiv': emit('shr', [n, const(k)], d=i.d)
            else: emit('and', [n, const(d-1)], d=i.d)
            return seq
        if d >= 0x80000000:
            # n/d is 0 or 1 when d has the top bit set.
            q = emit('cmp', [n, const(d)]); seq[-1].s = 'geu'
            if op == 'udiv': emit('copy', [q], d=i.d)
            else:
                t = emit('mul', [q, const(d)])
                emit('sub', [n, t], d=i.d)
            return seq
        m, add, k = magic_unsigned(d)
        t = emit('mulhu', [n, const(m)])
        if add:
            u = emit('sub', [n, t])
            u = emit('shr', [u, const(1)])
            t = emit('add', [u, t])
            q = emit('shr', [t, const(k)]) if k else t
        else:
            q = emit('shr', [t, const(k)]) if k else t
        if op == 'udiv': emit('copy', [q], d=i.d)
        else:
            t = emit('mul', [q, const(d)])
            emit('sub', [n, t], d=i.d)
        return seq
    if op in ('div', 'mod'):
        d = c(i.a[1])
        if d is None: return None
        d = s32(d)
        if d in (0, 1): return None
        n = i.a[0]
        if d == -1:
            if op == 'div': emit('neg', [n], d=i.d)
            else: emit('const', [], 0, d=i.d)
            return seq
        ad = abs(d)
        if is_pow2(ad):
            k = ad.bit_length() - 1
            # Bias negative dividends so the arithmetic shift truncates towards zero.
            if k == 1: bias = emit('shr', [n, const(31)])
            else:
                sgn = emit('sar', [n, const(31)])
                bias = emit('shr', [sgn, const(32-k)])
            t = emit('add', [n, bias])
            if op == 'div':
                q = emit('sar', [t, const(k)])
                if d < 0: q = emit('neg', [q])
                emit('copy', [q], d=i.d)
            else:
                m = emit('and', [t, const((-ad) & MASK)])
                emit('sub', [n, m], d=i.d)
            return seq
        if ad == 0x80000000:
            return None
        M, s = magic_signed(d)
        q = emit('mulhs', [n, const(M)])
        if d > 0 and s32(M) < 0: q = emit('add', [q, n])
        if d < 0 and s32(M) > 0: q = emit('sub', [q, n])
        if s: q = emit('sar', [q, const(s)])
        # Round towards zero: add the dividend's sign (d > 0) or the quotient's sign (d < 0).
        t = emit('shr', [n if d > 0 else q, const(31)])
        q = emit('add', [q, t])
        if op == 'div': emit('copy', [q], d=i.d)
        else:
            t = emit('mul', [q, const(d)])
            emit('sub', [n, t], d=i.d)
        return seq
    return None


# ---------------------------------------------------------------------------
# strength reduction of induction-variable products

def strength_reduce(fn):
    """Replace `iv * c' / `iv << k' inside a loop by a new induction variable.

    iv must be a header phi phi(init, iv + step) with constant step and the
    increment inside the loop; c must be a constant.
    """
    ir.link(fn)
    lps = ir.loops(fn)
    if not lps: return False
    for lp in lps: ensure_preheader(fn, lp)
    ir.link(fn); lps = ir.loops(fn)
    changed = False
    for lp in lps: ensure_preheader(fn, lp)
    defs = def_map(fn)
    ids = {b.id: b for b in fn.blocks}
    for lp in sorted(lps, key=lambda l: -l.depth):
        pre = lp.preheader
        if pre is None: continue
        header = lp.header
        members = [ids[x] for x in lp.blocks if x in ids]
        # Basic induction variables.
        ivs = {}
        for p in header.phis:
            if len(p.t) != 2: continue
            ins_k = [k for k, x in enumerate(p.t) if x.id in lp.blocks]
            if len(ins_k) != 1: continue
            k = ins_k[0]
            nxt = defs.get(p.a[k])
            if nxt is None or nxt.op != 'add' or nxt.a[0] != p.d or nxt.blk is None or nxt.blk.id not in lp.blocks: continue
            c = defs.get(nxt.a[1])
            if c is None or c.op != 'const': continue
            ivs[p.d] = (p, nxt, s32(c.imm), p.a[1-k], p.t[1-k])
        if not ivs: continue
        made = {}
        for b in members:
            for i in list(b.ins):
                # c >> iv, c << iv with constant c and |step| 1: shift the previous value by one.
                if i.op in ('shl', 'shr', 'sar') and i.a[1] in ivs and defs.get(i.a[0]) is not None \
                        and defs[i.a[0]].op == 'const' and ivs[i.a[1]][2] == 1:
                    p, nxt, step, init, _ = ivs[i.a[1]]
                    key = ('shift', i.op, i.a[0], p.d)
                    q = made.get(key)
                    if q is None:
                        # Only exact while the shift stays in 0..31: the IV must start >= 0,
                        # which holds for the loops this targets (x = 0..n); require a const init.
                        ci = defs.get(init)
                        if ci is None or ci.op != 'const' or not (0 <= s32(ci.imm) <= 31): continue
                        q0 = Ins(i.op, fn.vreg(), [i.a[0], init]); q0.blk = pre
                        pre.ins.insert(len(pre.ins)-1, q0)
                        qd = fn.vreg(); qn = fn.vreg()
                        one = Ins('const', fn.vreg(), (), 1)
                        # iv' = iv + 1: shl -> <<1, shr/sar -> >>1; iv' = iv - 1 is the inverse.
                        if step == 1: nop = i.op
                        else: nop = 'shl' if i.op != 'shl' else 'shr'
                        if nop == 'sar': nop = 'sar'
                        add = Ins(nop, qn, [qd, one.d])
                        blk = nxt.blk; pos = blk.ins.index(nxt) + 1
                        one.blk = add.blk = blk
                        blk.ins[pos:pos] = [one, add]
                        phi = Ins('phi', qd, [q0.d if x.id not in lp.blocks else qn for x in p.t])
                        phi.t = list(p.t); phi.blk = header
                        header.phis.append(phi)
                        for n_ in (q0, one, add, phi): defs[n_.d] = n_
                        q = made[key] = qd
                    i.op = 'copy'; i.a = [q]
                    changed = True
                    continue
                if i.op not in ('mul', 'shl') or i.a[0] not in ivs: continue
                c = defs.get(i.a[1])
                if c is None or c.op != 'const': continue
                factor = c.imm & MASK if i.op == 'mul' else (1 << (c.imm & 31)) & MASK
                p, nxt, step, init, _ = ivs[i.a[0]]
                key = (p.d, factor)
                q = made.get(key)
                if q is None:
                    # q = init*factor in the preheader; q' = q + step*factor beside iv'.
                    ci = Ins('const', fn.vreg(), (), factor); ci.blk = pre
                    q0 = Ins('mul', fn.vreg(), [init, ci.d]); q0.blk = pre
                    pre.ins[-1:-1] = [ci, q0]
                    qd = fn.vreg(); qn = fn.vreg()
                    sc = Ins('const', fn.vreg(), (), (step*factor) & MASK)
                    add = Ins('add', qn, [qd, sc.d])
                    blk = nxt.blk
                    pos = blk.ins.index(nxt) + 1
                    sc.blk = add.blk = blk
                    blk.ins[pos:pos] = [sc, add]
                    phi = Ins('phi', qd, [q0.d if x is p.t[[k for k, y in enumerate(p.t) if y.id not in lp.blocks][0]] else qn for x in p.t])
                    phi.t = list(p.t); phi.blk = header
                    header.phis.append(phi)
                    for n_ in (ci, q0, sc, add, phi): defs[n_.d] = n_
                    q = made[key] = qd
                i.op = 'copy'; i.a = [q]
                changed = True
    if changed:
        instcombine(fn)
        dce(fn)
    return changed


# ---------------------------------------------------------------------------
# if-conversion of clamps into MIN/MAX

def _offset_of(defs, v, base):
    """k when v == base + k (k in -1, 0, 1), else None."""
    if v == base: return 0
    i = defs.get(v)
    if i is not None and i.op == 'add' and i.a[0] == base:
        c = defs.get(i.a[1])
        if c is not None and c.op == 'const' and s32(c.imm) in (-1, 1): return s32(c.imm)
    k = _const_of(defs, v)
    b = _const_of(defs, base)
    if k is not None and b is not None and k - b in (-1, 0, 1): return k - b
    return None


def _const_of(defs, v):
    i = defs.get(v)
    return s32(i.imm) if i is not None and i.op == 'const' else None


def _is_neg_of(defs, v, x):
    i = defs.get(v)
    if i is None: return False
    if i.op == 'neg': return i.a[0] == x
    return i.op == 'sub' and i.a[1] == x and _const_of(defs, i.a[0]) == 0


def _clamp_select(defs, cc, x, y, vb, va):
    """(op, operands) computing `x cc y ? vb : va` as one min/max/abs, else None.

    Signed comparisons only (MIN/MAX are signed). vb may be y, y-1 or y+1
    when the comparison is then equivalent to comparing x with vb. va is x,
    or x already clamped from the other side by a constant that cannot
    change x whenever the condition holds (nested clamps).
    """
    for xx, yy, cond in ((x, y, cc), (y, x, ir.SWAP[cc])):
        if _const_of(defs, yy) == 0:
            # x < 0 ? -x : x (the boundary x == 0 selects either).
            if cond in ('lt', 'le') and va == xx and _is_neg_of(defs, vb, xx): return 'abs', [xx]
            if cond in ('gt', 'ge') and vb == xx and _is_neg_of(defs, va, xx): return 'abs', [xx]
        k = _offset_of(defs, vb, yy)
        if k is None: continue
        # Rewrite cond (xx cond yy) as a comparison of xx with z = yy + k.
        rel = {('gt', 0): 'gt', ('ge', 0): 'ge', ('ge', -1): 'gt', ('gt', 1): 'ge',
               ('lt', 0): 'lt', ('le', 0): 'le', ('le', 1): 'lt', ('lt', -1): 'le'}.get((cond, k))
        if rel is None: continue
        op = 'min' if rel in ('gt', 'ge') else 'max'
        if va == xx: return op, [xx, vb]
        # va = max(xx, L) with L <= z (or min(xx, U) with U >= z): whenever the
        # condition holds va == xx, and otherwise va is already within z.
        w = defs.get(va)
        if w is not None and w.op == ('max' if op == 'min' else 'min') and xx in w.a:
            bound = _const_of(defs, w.a[1] if w.a[0] == xx else w.a[0])
            z = _const_of(defs, vb)
            if bound is not None and z is not None and (bound <= z if op == 'min' else bound >= z):
                return op, [va, vb]
    return None


def _clamp_arm(a, s):
    """(join predecessor, join or 'ret', side instructions) for the edge a -> s."""
    if s is not a and len(s.preds) == 1 and not s.phis and s.term.op in ('jmp', 'ret'):
        return s, (s.term.t[0] if s.term.op == 'jmp' else 'ret'), s.ins[:-1]
    return a, s, []


def minmax(fn):
    """If-convert branches that only pick a clamp or absolute value into min/max/abs.

    Handles triangles and diamonds whose join has any number of predecessors
    (one phi differs between the two arms), and diamonds whose arms both
    return, so early-return and else-if clamps become straight-line code.
    Side blocks may hold a few pure instructions; those are hoisted.
    """
    ir.link(fn)
    defs = def_map(fn)
    changed = False
    for a in list(fn.blocks):
        t = a.term
        if t is None or t.op != 'br' or t.s not in ('lt', 'le', 'gt', 'ge'): continue
        (pT, jT, eT), (pF, jF, eF) = _clamp_arm(a, t.t[0]), _clamp_arm(a, t.t[1])
        if pT is pF or jT is not jF and not (jT == 'ret' == jF) or jT is a: continue
        extra = eT + eF
        if len(extra) > 3 or not all(is_pure(i) and i.op not in ir.TRAPPING for i in extra): continue
        x, y = t.a
        if jT == 'ret':
            if not pT.term.a or not pF.term.a: continue
            sel = _clamp_select(defs, t.s, x, y, pT.term.a[0], pF.term.a[0])
            if sel is None: continue
            phis, J = [], None
        else:
            J = jT
            phis = [(p, p.a[p.t.index(pT)], p.a[p.t.index(pF)]) for p in J.phis]
            diff = [e for e in phis if e[1] != e[2]]
            if len(diff) != 1: continue
            sel = _clamp_select(defs, t.s, x, y, diff[0][1], diff[0][2])
            if sel is None: continue
        # Hoist the side instructions, then select.
        for i in extra:
            i.blk = a; a.ins.insert(len(a.ins)-1, i)
        op, args = sel
        d = fn.vreg()
        m = Ins(op, d, args); m.blk = a
        defs[d] = m
        a.ins.insert(len(a.ins)-1, m)
        if J is None:
            a.ins[-1] = Ins('ret', None, [d])
        else:
            a.ins[-1] = Ins('jmp', t=[J])
            for p, vT, vF in phis:
                keep = [(q, v) for q, v in zip(p.t, p.a) if q is not pT and q is not pF]
                keep.append((a, d if vT != vF else vT))
                p.t = [q for q, _ in keep]; p.a = [v for _, v in keep]
        a.ins[-1].blk = a
        changed = True
        ir.link(fn)
        if J is not None and J.preds == [a]:
            # A single predecessor remains: the phis become copies.
            J.ins[0:0] = [Ins('copy', p.d, [p.a[0]]) for p in J.phis]
            for i in J.ins: i.blk = J
            for p in J.phis: defs[p.d] = next(i for i in J.ins if i.d == p.d)
            J.phis = []
    return changed


# ---------------------------------------------------------------------------
# Q16 multiplies by constants as one fractional multiply

def fractional_q16(fn):
    """mulq16(x, c) with |c| < 65536 becomes mulfr(x, c << 15).

    The truncating fractional multiply returns (x * (c << 15)) >> 31, which
    equals (x * c) >> 16 in one instruction instead of a 64-bit product and a
    long shift. c = -65536 is excluded so that the product never needs the
    -1 * -1 fractional special case.
    """
    defs = def_map(fn)
    made = {}
    changed = False
    for b in fn.blocks:
        for i in b.ins:
            if i.op != 'mulq16': continue
            for k in (1, 0):
                c = defs.get(i.a[k])
                if c is not None and c.op == 'const' and -65535 <= s32(c.imm) <= 65535: break
            else:
                continue
            n = made.get(c.d)
            if n is None:
                n = made[c.d] = fn.vreg()
                ci = Ins('const', n, (), (c.imm << 15) & MASK); ci.blk = c.blk
                c.blk.ins.insert(c.blk.ins.index(c) + 1, ci)
            i.op = 'mulfr'; i.a = [i.a[1-k], n]
            changed = True
    if changed: dce(fn)
    return changed


# ---------------------------------------------------------------------------
# if-conversion to conditional moves (inside loops)

def if_convert(fn, max_ops=3, max_phis=3):
    """Turn small branch triangles/diamonds inside loops into `sel' (conditional moves).

    Side blocks may only hold pure, non-trapping instructions; those are
    hoisted above the branch. Loop bodies become single blocks, which enables
    unrolling and software pipelining.
    """
    ir.link(fn)
    ir.loops(fn)
    changed = False
    for a in list(fn.blocks):
        t = a.term
        if t is None or t.op != 'br' or a.depth == 0: continue
        x, y = t.a
        T, F = t.t
        # Identify the join block and the side blocks on each arm.
        def side(b, join):
            return b is not join and len(b.preds) == 1 and b.term.op == 'jmp' and b.term.t[0] is join and not b.phis
        join = None
        if side(T, F): join = F; sides = {'T': T, 'F': None}
        elif side(F, T): join = T; sides = {'T': None, 'F': F}
        elif T.term.op == 'jmp' and F.term.op == 'jmp' and T.term.t[0] is F.term.t[0] and side(T, T.term.t[0]) and side(F, F.term.t[0]):
            join = T.term.t[0]; sides = {'T': T, 'F': F}
        if join is None or join is a: continue
        blocks = [b for b in sides.values() if b is not None]
        extra = [i for b in blocks for i in b.ins if i is not b.term]
        if len(extra) > max_ops or not all(is_pure(i) and i.op not in ir.TRAPPING for i in extra): continue
        if not join.phis or len(join.phis) > max_phis: continue
        preds = set(join.preds)
        expect = {sides['T'] or a, sides['F'] or a}
        if preds != expect or len(join.preds) != 2: continue
        # Hoist the side instructions, then select between the arm values.
        for b in blocks:
            for i in b.ins[:-1]:
                i.blk = a; a.ins.insert(len(a.ins)-1, i)
            b.ins = [b.term]
        tpred = sides['T'] or a
        fpred = sides['F'] or a
        sels = []
        for p in join.phis:
            vt = p.a[p.t.index(tpred)]; vf = p.a[p.t.index(fpred)]
            s_ = Ins('sel', p.d, [x, y, vt, vf], 0, t.s); s_.blk = a
            sels.append(s_)
        a.ins[-1:-1] = sels
        join.phis = []
        a.ins[-1] = Ins('jmp', t=[join]); a.ins[-1].blk = a
        changed = True
    if changed: ir.link(fn)
    return changed


# ---------------------------------------------------------------------------
# live-range shortening for loop-carried increments

def sink_increments(fn):
    """Move `d = a + c` (a a phi of the block) below a's remaining address uses.

    Memory accesses keep using `a`; an eq/ne branch on `a` against a constant
    is rewritten to test `d` against the adjusted constant. Afterwards `a` and
    `d` are not live together, so phi copies coalesce and the backend can use
    post-modify addressing.
    """
    defs = def_map(fn)
    changed = False
    for b in fn.blocks:
        phis = {p.d for p in b.phis}
        if not phis: continue
        ins = b.ins
        k = 0
        while k < len(ins):
            i = ins[k]
            if not (i.op == 'add' and i.a[0] in phis):
                k += 1; continue
            c = defs.get(i.a[1])
            if c is None or c.op != 'const':
                k += 1; continue
            a, d = i.a[0], i.d
            later = [j for j in range(k+1, len(ins)) if a in ins[j].a]
            if not later:
                k += 1; continue
            term = ins[-1]
            body_uses = [j for j in later if j != len(ins)-1]
            ok = all(ins[j].op in ('load', 'store', 'loadq', 'storeq') and ins[j].a[0] == a and a not in ins[j].a[1:] for j in body_uses)
            rewrite_term = later[-1] == len(ins)-1
            if rewrite_term:
                y = defs.get(term.a[1]) if term.op == 'br' else None
                ok = ok and term.op == 'br' and term.s in ('eq', 'ne') and term.a[0] == a and term.a[1] != a                      and y is not None and y.op == 'const'
            last = body_uses[-1] if body_uses else k
            if ok and any(d in ins[j].a for j in range(k+1, last+1)): ok = False
            if not ok:
                k += 1; continue
            if rewrite_term:
                n = fn.vreg()
                ci = Ins('const', n, (), (defs[term.a[1]].imm + c.imm) & MASK); ci.blk = b
                defs[n] = ci
                term.a = [d, n]
                ins.insert(len(ins)-1, ci)
            if last > k:
                ins.pop(k)
                ins.insert(last, i)   # indices shifted by the pop: lands after the last use
                changed = True
                continue
            changed = True
            k += 1
    return changed


# ---------------------------------------------------------------------------
# driver

import os
DISABLED = set(filter(None, os.environ.get('TS201_DISABLE', '').split(',')))


def optimize(fn, options=None, level=1):
    from . import ssa
    # Only the scalar passes need the stripped-down debug pipeline; any other
    # knob (a backend pass) must leave this pipeline unchanged.
    if DISABLED & {'cfg', 'sccp', 'instcombine', 'gvn', 'dce', 'licm', 'legalize'}:
        return _optimize_debug(fn, options, level)
    simplify_cfg(fn)
    ssa.build(fn)
    for it in range(4):
        changed = sccp(fn)
        changed |= instcombine(fn)
        defs = def_map(fn)
        alias = AliasInfo(fn, defs, options)
        changed |= gvn(fn, alias)
        changed |= dce(fn)
        changed |= simplify_cfg(fn)
        changed |= minmax(fn)
        if 'range' not in DISABLED: changed |= fuse_range_checks(fn)
        if level >= 2 and 'ifconv' not in DISABLED:
            changed |= if_convert(fn)
        if not changed: break
    defs = def_map(fn)
    alias = AliasInfo(fn, defs, options)
    if level >= 1 and 'sr' not in DISABLED and strength_reduce(fn):
        gvn(fn, AliasInfo(fn, def_map(fn), options))
        dce(fn)
    defs = def_map(fn)
    alias = AliasInfo(fn, defs, options)
    if licm(fn, alias):
        instcombine(fn)
        defs = def_map(fn)
        gvn(fn, AliasInfo(fn, defs, options))
        dce(fn)
    if legalize(fn):
        instcombine(fn)
        defs = def_map(fn)
        gvn(fn, AliasInfo(fn, defs, options))
        dce(fn)
        defs = def_map(fn)
        licm(fn, AliasInfo(fn, defs, options))
        dce(fn)
    if 'mulfr' not in DISABLED: fractional_q16(fn)
    simplify_cfg(fn)
    sink_increments(fn)
    return fn


def _optimize_debug(fn, options, level):
    """optimize() with passes named in TS201_DISABLE switched off (debugging aid)."""
    from . import ssa
    on = lambda name: name not in DISABLED
    if on('cfg'): simplify_cfg(fn)
    ssa.build(fn)
    for it in range(4):
        changed = False
        if on('sccp'): changed |= sccp(fn)
        if on('instcombine'): changed |= instcombine(fn)
        if on('gvn'):
            defs = def_map(fn)
            changed |= gvn(fn, AliasInfo(fn, defs, options))
        if on('dce'): changed |= dce(fn)
        if on('cfg'): changed |= simplify_cfg(fn)
        if not changed: break
    if on('licm'):
        defs = def_map(fn)
        licm(fn, AliasInfo(fn, defs, options))
    if on('legalize'): legalize(fn)
    if on('instcombine'): instcombine(fn)
    if on('dce'): dce(fn)
    ir.link(fn)
    return fn
