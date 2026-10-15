"""Counted-loop transformations on SSA: hardware loop counters and unrolling.

A loop qualifies when its single latch ends in a branch comparing an
induction variable `iv' = iv + s` (s = +1/-1) with a loop-invariant bound and
the trip count N >= 1 is known in the preheader:

  continue while iv' != K          N = (K - iv0) * s        (mod 2^32)
  continue while iv' <  K (s=+1)   N = K - iv0, guard proves iv0 < K
  continue while iv' >  K (s=-1)   N = iv0 - K, guard proves iv0 > K
  (and the <=, >=, unsigned forms)

The latch branch becomes `IF NLCnE, JUMP' (decrement-and-branch, no compare).
LC0 is used unless the loop contains a division macro (which uses LC0), in
which case LC1 is used. Loops containing calls are left alone because a
callee may use the counters.

Single-block counted loops with small bodies are also unrolled: a remainder
loop runs N mod U iterations, then the main loop runs N / U iterations of U
body copies, giving the scheduler independent work to overlap.
"""
from __future__ import annotations
from . import ir, opt
from .ir import Ins, MASK, s32

UNROLL_MAX_BODY = 28     # IR instructions in the loop body
UNROLL_FACTOR = 4
FULL_UNROLL_MAX = 8      # constant trip counts up to this are unrolled completely
FULL_UNROLL_BODY = 200   # ... when the unrolled code stays this small


def _const(defs, v):
    i = defs.get(v)
    return i.imm & MASK if i is not None and i.op == 'const' else None


def analyze(fn, lp, defs, ids):
    """Return (latch, iv0, K, step, cc, header_on_true) or None."""
    if len(lp.latches) != 1: return None
    latch = lp.latches[0]
    t = latch.term
    if t.op != 'br': return None
    header = lp.header
    if header not in t.t: return None
    on_true = t.t[0] is header
    other = t.t[1] if on_true else t.t[0]
    if other.id in lp.blocks: return None
    cc = t.s if on_true else ir.NEGATE[t.s]
    x, k = t.a
    xi = defs.get(x)
    if xi is None or xi.op != 'add': return None
    phi = defs.get(xi.a[0])
    step = _const(defs, xi.a[1])
    if step is None or s32(step) not in (1, -1) or phi is None or phi.op != 'phi' or phi.blk is not header: return None
    step = s32(step)
    # iv' must feed the phi from the latch; the other operand is the start value.
    if len(phi.t) != 2 or latch not in phi.t: return None
    if phi.a[phi.t.index(latch)] != x: return None
    iv0 = phi.a[1 - phi.t.index(latch)]
    kd = defs.get(k)
    if kd is not None and kd.op != 'const' and kd.blk is not None and kd.blk.id in lp.blocks: return None
    return latch, iv0, k, step, cc, on_true, phi, xi


def _guarded(lp, iv0, k, cc, defs):
    """Does the loop entry require `iv0 cc k'?"""
    c0, ck = _const(defs, iv0), _const(defs, k)
    if c0 is not None and ck is not None:
        return ir.compare(cc, c0, ck)
    pre = lp.preheader
    if pre is None or len(pre.preds) != 1: return False
    g = pre.preds[0]
    t = g.term
    if t is None or t.op != 'br': return False
    if t.t[0] is pre and t.t[1] is not pre: gcc = t.s
    elif t.t[1] is pre and t.t[0] is not pre: gcc = ir.NEGATE[t.s]
    else: return False
    a, b = t.a
    return (a, b, gcc) == (iv0, k, cc) or (b, a, ir.SWAP[gcc]) == (iv0, k, cc)


def trip_count(lp, info, defs):
    """(kind, extra): N = s*(K - iv0) + extra, or None when not provable."""
    latch, iv0, k, step, cc, on_true, phi, xi = info
    if cc == 'ne': return 0
    if step == 1 and cc in ('lt', 'ltu') and _guarded(lp, iv0, k, cc, defs): return 0
    if step == 1 and cc in ('le', 'leu') and _guarded(lp, iv0, k, cc, defs): return 1
    if step == -1 and cc in ('gt', 'gtu') and _guarded(lp, iv0, k, cc, defs): return 0
    if step == -1 and cc in ('ge', 'geu') and _guarded(lp, iv0, k, cc, defs): return 1
    return None


def _emit_before_term(b, ins):
    for i in ins: i.blk = b
    b.ins[-1:-1] = ins


def transform(fn, options=None, unroll=True):
    ir.link(fn)
    lps = ir.loops(fn)
    changed = False
    for lp in lps:
        opt.ensure_preheader(fn, lp)
    ir.link(fn)
    lps = ir.loops(fn)
    ids = {b.id: b for b in fn.blocks}
    defs = opt.def_map(fn)
    for lp in lps:
        if lp.children: continue          # innermost loops only
        opt.ensure_preheader(fn, lp)
        members = [ids[x] for x in lp.blocks if x in ids]
        if any(i.op in ('call', 'jtab', 'qcopy') for b in members for i in b.ins): continue
        has_div = any(i.op in ('div', 'udiv', 'mod', 'umod') for b in members for i in b.ins)
        info = analyze(fn, lp, defs, ids)
        if info is None: continue
        extra = trip_count(lp, info, defs)
        if extra is None: continue
        latch, iv0, k, step, cc, on_true, phi, xi = info
        pre = lp.preheader
        # N = step*(K - iv0) + extra, computed in the preheader.
        seq = []

        def emit(op, a, imm=0):
            n = Ins(op, fn.vreg(), a, imm); seq.append(n); defs[n.d] = n
            return n.d

        def const(v):
            return emit('const', [], v & MASK)
        ck, c0 = _const(defs, k), _const(defs, iv0)
        exit_ = latch.term.t[1] if on_true else latch.term.t[0]
        if unroll and ck is not None and c0 is not None and lp.header is latch:
            N = (step * (ck - c0) + extra) & MASK
            body_n = sum(1 for i in latch.ins if i is not latch.term)
            if 1 <= N <= FULL_UNROLL_MAX and N * body_n <= FULL_UNROLL_BODY and _unrollable(fn, lp, latch, exit_):
                _full_unroll(fn, lp, latch, exit_, N)
                changed = True
                ir.link(fn)
                defs = opt.def_map(fn)
                ids = {b.id: b for b in fn.blocks}
                continue
        if unroll and ck is not None and c0 is not None and lp.header is not latch and 'unrollm' not in opt.DISABLED:
            N = (step * (ck - c0) + extra) & MASK
            body_n = sum(len(b.ins) + len(b.phis) for b in members)
            if 1 <= N <= FULL_UNROLL_MAX and N * body_n <= FULL_UNROLL_BODY and _full_unroll_multi(fn, lp, ids, latch, exit_, N):
                changed = True
                ir.link(fn)
                defs = opt.def_map(fn)
                ids = {b.id: b for b in fn.blocks}
                continue
        if ck is not None and c0 is not None:
            N = (step * (ck - c0) + extra) & MASK
            n = const(N)
        else:
            # Constant bounds may be defined inside the loop: rematerialize them here.
            kk = const(ck) if ck is not None else k
            i0 = const(c0) if c0 is not None else iv0
            n = emit('sub', [kk, i0] if step == 1 else [i0, kk])
            if extra: n = emit('add', [n, const(extra)])
        _emit_before_term(pre, seq)
        exit_ = latch.term.t[1] if on_true else latch.term.t[0]
        lc = 1 if has_div else 0
        single = lp.header is latch
        if single and 'version' not in opt.DISABLED:
            v = version_for_alias(fn, lp, latch, exit_, n, lc, defs, options)
            if v is not None:
                exit_ = v
                pre = lp.preheader
                defs = opt.def_map(fn)
        body = sum(len(b.ins) for b in members)
        if unroll and single and body <= UNROLL_MAX_BODY and not has_div and not _one_packet_body(latch, defs)                 and _unrollable(fn, lp, latch, exit_):
            _unroll(fn, lp, latch, exit_, n, lc, UNROLL_FACTOR, defs)
        else:
            pre.ins.insert(len(pre.ins)-1, _mk(Ins('setlc', None, [n], lc), pre))
            latch.ins[-1] = _mk(Ins('lcbr', None, [], lc, None, [lp.header, exit_]), latch)
        changed = True
        ir.link(fn)
        defs = opt.def_map(fn)
        ids = {b.id: b for b in fn.blocks}
    if changed:
        opt.dce(fn)
        ir.link(fn)
        # Fold the unrolled copies' offset chains (p+1+1 -> p+2) before pipelining.
        opt.instcombine(fn); opt.dce(fn); ir.link(fn)
        if unroll and pipeline_all(fn, skip=getattr(fn, 'remainder_loops', set())):
            opt.dce(fn)
            ir.link(fn)
    return changed


def _one_packet_body(L, defs):
    """A body that is only a load and/or a store besides constant-step inductions
    already runs one iteration per packet (post-increment addressing on J and K);
    unrolling it only adds a remainder loop."""
    phis = {p.d for p in L.phis}
    work = []
    for i in L.ins:
        if i is L.term or i.op == 'const': continue
        if i.op in ('add', 'sub') and i.a[0] in phis and _const(defs, i.a[1]) is not None: continue
        work.append(i)
    return len(work) <= 2 and all(i.op in ('load', 'store', 'loadq', 'storeq') for i in work)         and sum(1 for i in work if i.op in ('load', 'loadq')) <= 1


def _mk(i, b):
    i.blk = b
    return i


# ---------------------------------------------------------------------------
# loop versioning for memory disambiguation

VERSION_MAX_CHECKS = 4
_version_serial = [0]


def _value_range(v, L, defs, ivs, depth=0):
    """Bound v over all iterations of single-block loop L.

    Returns (anchor, lo, hi) meaning anchor + lo <= v <= anchor + hi, where
    anchor is None (a constant range), a loop-invariant vreg, or ('iv', phi)
    for a header induction variable (its per-iteration value). None if unknown.
    """
    if depth > 12: return None
    i = defs.get(v)
    if i is None: return (v, 0, 0)
    if i.op == 'const': return (None, s32(i.imm), s32(i.imm))
    if i.blk is not L: return (v, 0, 0)
    if i.op == 'phi':
        return (('iv', v), 0, 0) if v in ivs else None
    if i.op == 'copy': return _value_range(i.a[0], L, defs, ivs, depth+1)
    op = i.op
    if op in ('add', 'sub'):
        x = _value_range(i.a[0], L, defs, ivs, depth+1)
        y = _value_range(i.a[1], L, defs, ivs, depth+1)
        if x is None or y is None: return None
        if op == 'sub':
            if y[0] is not None: return None
            return (x[0], x[1] - y[2], x[2] - y[1])
        if x[0] is not None and y[0] is not None: return None
        return (x[0] if x[0] is not None else y[0], x[1] + y[1], x[2] + y[2])
    if op not in ('and', 'shr', 'sar', 'shl', 'mul', 'or'): return None
    cb = defs.get(i.a[1])
    cb = cb.imm & MASK if cb is not None and cb.op == 'const' else None
    if op == 'and':
        ca = defs.get(i.a[0])
        if cb is None and ca is not None and ca.op == 'const': cb = ca.imm & MASK
        if cb is not None and cb < 0x80000000: return (None, 0, cb)
        return None
    x = _value_range(i.a[0], L, defs, ivs, depth+1)
    nonneg = x is not None and x[0] is None and x[1] >= 0
    if op == 'shr' and cb is not None and 1 <= cb <= 31:
        if nonneg: return (None, x[1] >> cb, x[2] >> cb)
        return (None, 0, MASK >> cb)
    if op == 'sar' and cb is not None and nonneg: return (None, x[1] >> cb, x[2] >> cb)
    if op == 'shl' and cb is not None and nonneg and cb <= 31 and x[2] << cb < 0x80000000:
        return (None, x[1] << cb, x[2] << cb)
    if op == 'mul' and cb is not None and nonneg and cb < 0x10000 and x[2] * cb < 0x80000000:
        return (None, x[1] * cb, x[2] * cb)
    if op == 'or' and nonneg:
        y = _value_range(i.a[1], L, defs, ivs, depth+1)
        if y is not None and y[0] is None and y[1] >= 0:
            return (None, 0, (1 << max(x[2], y[2]).bit_length()) - 1)
    return None


def version_for_alias(fn, lp, L, exit_, n, lc, defs, options):
    """Split a single-block counted loop into a fast copy, whose pointer groups
    are proven disjoint at run time, and the original as fallback.

    Each memory access is bounded by `anchor + [lo, hi]` over the whole loop
    (anchor: a loop-invariant pointer, or an induction pointer whose range
    follows from the trip count `n`). Accesses with the same anchor form a
    group. When groups that may alias (one of them stored to) have disjoint
    address intervals, the fast copy runs with its accesses tagged
    `restrict:v<k>:g<j>`: different groups of one version never alias.
    Returns the new loop exit (the join block) or None.
    """
    pre = lp.preheader
    if pre is None or pre.term is None or pre.term.op != 'jmp' or pre.term.t[0] is not L: return None
    mem = [i for i in L.ins if i.op in ('load', 'store', 'loadq', 'storeq')]
    if not any(i.op in ('store', 'storeq') for i in mem): return None
    if any(i.vol or (isinstance(i.s, str) and i.s.startswith('restrict:')) for i in mem): return None
    alias = opt.AliasInfo(fn, defs, options)
    addr = {id(i): alias.addr(i) for i in mem}
    # Header induction variables p = phi(p0, p + c).
    ivs = {}
    for p in L.phis:
        if len(p.t) != 2 or L not in p.t or pre not in p.t: continue
        nxt = defs.get(p.a[p.t.index(L)])
        if nxt is None or nxt.op != 'add' or nxt.a[0] != p.d or nxt.blk is not L: continue
        c = defs.get(nxt.a[1])
        if c is None or c.op != 'const' or s32(c.imm) == 0: continue
        ivs[p.d] = (p.a[p.t.index(pre)], s32(c.imm))
    groups = {}     # anchor -> [lo, hi, [accesses]]
    unbounded = []
    for i in mem:
        r = _value_range(i.a[0], L, defs, ivs)
        if r is None or r[0] is None:
            unbounded.append(i); continue
        w = 4 if i.op in ('loadq', 'storeq') else 1
        lo, hi = r[1] + i.imm, r[2] + i.imm + w - 1
        g = groups.setdefault(r[0], [lo, hi, []])
        g[0] = min(g[0], lo); g[1] = max(g[1], hi); g[2].append(i)
    keys = list(groups)
    if len(keys) < 2: return None
    checks = []
    for a_k in range(len(keys)):
        for b_k in range(a_k+1, len(keys)):
            A, B = groups[keys[a_k]], groups[keys[b_k]]
            if any((x.op in ('store', 'storeq') or y.op in ('store', 'storeq')) and alias.may_alias(addr[id(x)], addr[id(y)], cross=True)
                   for x in A[2] for y in B[2]):
                checks.append((keys[a_k], keys[b_k]))
    if not checks or len(checks) > VERSION_MAX_CHECKS: return None
    if any(abs(g[0]) >= 0x40000000 or abs(g[1]) >= 0x40000000 for g in groups.values()): return None
    # --- run-time interval tests -----------------------------------------
    # Group k covers [start, start + len] (mod 2^32). Two groups are disjoint
    # iff (b - a) >u len_a and (a - b) >u len_b; this is exact under address
    # wrap-around. Every access of a counted single-block loop executes on
    # each iteration, so in a defined execution a pointer group spans one
    # object and its length cannot overflow.
    pre1 = fn.block(); pre2 = fn.block(); L2 = fn.block()
    cur = [pre]
    body = []

    def emit(op, a, imm=0, s=None):
        x = Ins(op, fn.vreg(), a, imm, s); x.blk = cur[0]; body.append(x); defs[x.d] = x
        return x.d

    def const(v):
        return emit('const', [], v & MASK)

    def test(x, y, cc):
        """Continue in a new block when `x cc y`, otherwise take the original loop."""
        nxt = fn.block()
        cur[0].ins[-1:] = body + [_mk(Ins('br', None, [x, y], 0, cc, [nxt, pre2]), cur[0])]
        body.clear()
        nxt.ins.append(_mk(Ins('jmp', t=[L]), nxt))
        cur[0] = nxt
    span = {}
    n1 = None
    used = {k for pair in checks for k in pair}
    bounds = {}
    for k in keys:
        if k not in used: continue
        lo, hi, _ = groups[k]
        if isinstance(k, tuple):
            p0, c = ivs[k[1]]
            if n1 is None: n1 = emit('sub', [n, const(1)])
            t = span.get(abs(c))
            if t is None: t = span[abs(c)] = emit('mul', [n1, const(abs(c))])
            start = emit('sub', [p0, t]) if c < 0 else p0
            length = emit('add', [t, const(hi - lo)]) if hi != lo else t
        else:
            start = k; length = const(hi - lo)
        if lo: start = emit('add', [start, const(lo)])
        bounds[k] = (start, length)
    for a_k, b_k in checks:
        (a, la), (b, lb) = bounds[a_k], bounds[b_k]
        test(emit('sub', [b, a]), la, 'gtu')
        test(emit('sub', [a, b]), lb, 'gtu')
    # --- CFG: pre -> tests -> pre1 -> L (fast) | pre2 -> L2 (original) -> J
    J = ir.split_edge(fn, L, exit_)
    last = cur[0]
    last.ins[-1:] = body + [_mk(Ins('jmp', t=[pre1]), last)]
    pre1.ins.append(_mk(Ins('jmp', t=[L]), pre1))
    for p in L.phis:
        p.t = [pre1 if x is pre else x for x in p.t]
    vmap = {}
    for p in L.phis: vmap[p.d] = fn.vreg()
    for i in L.ins:
        if i.d is not None: vmap[i.d] = fn.vreg()
    for p in L.phis:
        q = Ins('phi', vmap[p.d], [vmap.get(x, x) for x in p.a]); q.t = [pre2 if x is pre1 else L2 for x in p.t]
        q.blk = L2; L2.phis.append(q)
    for i in L.ins:
        if i is L.term: continue
        c = Ins(i.op, vmap.get(i.d) if i.d is not None else None, [vmap.get(x, x) for x in i.a], i.imm, i.s, [], i.vol)
        c.blk = L2; L2.ins.append(c)
    pre2.ins.append(_mk(Ins('setlc', None, [n], lc), pre2))
    pre2.ins.append(_mk(Ins('jmp', t=[L2]), pre2))
    L2.ins.append(_mk(Ins('lcbr', None, [], lc, None, [L2, J]), L2))
    if not hasattr(fn, 'remainder_loops'): fn.remainder_loops = set()
    fn.remainder_loops.add(L2.id)
    # Values of L used after the loop merge in J.
    inside = set(vmap)
    merged = {}
    for b in list(fn.blocks):
        if b in (L, L2, J): continue
        for i in b.phis + b.ins:
            for k, x in enumerate(i.a):
                if x in inside:
                    m = merged.get(x)
                    if m is None:
                        q = Ins('phi', fn.vreg(), [x, vmap[x]]); q.t = [L, L2]; q.blk = J
                        J.phis.append(q); m = merged[x] = q.d
                    i.a[k] = m
    # Tag the fast copy.
    _version_serial[0] += 1
    tag = f'restrict:v{_version_serial[0]}'
    for gk, k in enumerate(keys):
        if k in used:
            for i in groups[k][2]: i.s = f'{tag}:g{gk}'
    lp.preheader = pre1
    ir.link(fn)
    return J


_retag_serial = [0]


def _retag(s, memo):
    """Straight-line memcpy tags (restrict:s<n>) and inlined bodies' tags (.i<n>) name
    one dynamic call: a body copy gets fresh ones."""
    if not (isinstance(s, str) and s.startswith('restrict:')): return s
    p, part = s.rsplit(':', 1)
    if not (p.startswith('restrict:s') or '.i' in p): return s
    if p not in memo:
        _retag_serial[0] += 1
        memo[p] = f'{p}.c{_retag_serial[0]}'
    return memo[p] + ':' + part


def _full_unroll(fn, lp, L, exit_, N):
    """Replace a single-block loop with a constant trip count N by N body copies."""
    pre = lp.preheader
    phis = list(L.phis)
    body = [i for i in L.ins if i is not L.term]
    carried = [p.a[p.t.index(L)] for p in phis]
    vals = [p.a[1 - p.t.index(L)] for p in phis]
    B = fn.block()
    for _ in range(N):
        vmap = {p.d: v for p, v in zip(phis, vals)}
        memo = {}
        for i in body:
            c = Ins(i.op, fn.vreg() if i.d is not None else None, [vmap.get(x, x) for x in i.a], i.imm, _retag(i.s, memo), [], i.vol)
            if i.d is not None: vmap[i.d] = c.d
            c.blk = B; B.ins.append(c)
        vals = [vmap.get(x, x) for x in carried]
    B.ins.append(_mk(Ins('jmp', t=[exit_]), B))
    pre.term.t = [B if x is L else x for x in pre.term.t]
    mapping = dict(zip(carried, vals))
    for b in fn.blocks:
        if b in (L, B): continue
        for i in b.phis:
            i.t = [B if x is L else x for x in i.t]
            i.a = [mapping.get(x, x) for x in i.a]
        for i in b.ins:
            i.a = [mapping.get(x, x) for x in i.a]
    fn.blocks.remove(L)


def _full_unroll_multi(fn, lp, ids, latch, exit_, N):
    """Fully unroll a multi-block loop with constant trip count N (it may have
    early exits): N chained copies of the body, the last latch jumping to exit_.
    Exposes per-iteration invariants (e.g. a peeled first compare) to LICM."""
    H, pre = lp.header, lp.preheader
    inloop = set(lp.blocks)
    blocks = [b for b in fn.blocks if b.id in inloop]
    if len(H.preds) != 2 or pre not in H.preds or latch not in H.preds: return False
    if any(b.term is None or b.term.op not in ('br', 'jmp') for b in blocks): return False
    if any(H in b.term.t for b in blocks if b is not latch): return False
    defined = {i.d for b in blocks for i in b.ins if i.d is not None} | {p.d for b in blocks for p in b.phis}
    # Loop values may leave only through exit-block phis (one version per copy).
    for b in fn.blocks:
        if b.id in inloop: continue
        for i in b.ins:
            if any(x in defined for x in i.a): return False
        for p in b.phis:
            if any(x in defined and pred.id not in inloop for pred, x in zip(p.t, p.a)): return False
    hphis = list(H.phis)
    init = {p.d: p.a[p.t.index(pre)] for p in hphis}
    carried = {p.d: p.a[p.t.index(latch)] for p in hphis}
    copies = []
    prev = None
    for k in range(N):
        bmap = {b: fn.block() for b in blocks}
        vmap = {p.d: (init[p.d] if k == 0 else prev.get(carried[p.d], carried[p.d])) for p in hphis}
        for b in blocks:
            for p in b.phis:
                if b is not H: vmap[p.d] = fn.vreg()
            for i in b.ins:
                if i.d is not None: vmap[i.d] = fn.vreg()
        memo = {}
        for b in blocks:
            nb = bmap[b]
            for p in b.phis:
                if b is H: continue
                c = Ins('phi', vmap[p.d], [vmap.get(x, x) for x in p.a]); c.t = [bmap[t] for t in p.t]; c.blk = nb
                nb.phis.append(c)
            for i in b.ins:
                if i is b.term: continue
                c = Ins(i.op, vmap[i.d] if i.d is not None else None, [vmap.get(x, x) for x in i.a], i.imm, _retag(i.s, memo), [], i.vol)
                c.blk = nb; nb.ins.append(c)
            t = b.term
            if b is latch:
                c = Ins('jmp', t=[None])          # target set below
            else:
                c = Ins(t.op, None, [vmap.get(x, x) for x in t.a], t.imm, t.s, [bmap.get(x, x) for x in t.t], t.vol)
            c.blk = nb; nb.ins.append(c)
        copies.append((bmap, vmap))
        prev = vmap
    for k, (bmap, vmap) in enumerate(copies):
        bmap[latch].ins[-1].t = [copies[k+1][0][H] if k + 1 < N else exit_]
    # Exit-block phis: one operand per copy of each exiting edge (the latch's
    # exit edge survives only in the last copy).
    for b in fn.blocks:
        if b.id in inloop or any(b in bm.values() for bm, _ in copies[:1]): continue
        for p in b.phis:
            ts, as_ = [], []
            for pred, x in zip(p.t, p.a):
                if pred.id not in inloop:
                    ts.append(pred); as_.append(x); continue
                chosen = copies[-1:] if pred is latch else copies
                for bmap, vmap in chosen:
                    ts.append(bmap[pred]); as_.append(vmap.get(x, x))
            p.t, p.a = ts, as_
    pre.ins[-1].t = [copies[0][0][H] if x is H else x for x in pre.ins[-1].t]
    fn.blocks = [b for b in fn.blocks if b.id not in inloop]
    return True


# ---------------------------------------------------------------------------
# two-stage software pipelining of single-block hardware loops

PIPELINE_MIN_S1 = 3


def pipeline_all(fn, skip=()):
    """Pipeline every single-block counted loop (self-looping `lcbr')."""
    changed = False
    skip = set(skip)
    for b in list(fn.blocks):
        t = b.term
        if t is None or t.op != 'lcbr' or t.t[0] is not b or b.id in skip: continue
        if 'mpipe' not in opt.DISABLED and _pipeline_multi(fn, b, skip):
            changed = True
        elif _pipeline(fn, b):
            changed = True
    if changed: ir.link(fn)
    return changed


# ---------------------------------------------------------------------------
# multi-stage software pipelining

PIPE_MAX_STAGES = 4
_COMPUTE = {'shl', 'shr', 'sar', 'mul', 'and', 'or', 'xor', 'andn', 'min', 'max', 'neg', 'not', 'abs',
            'sel', 'cmp', 'mulhs', 'mulhu', 'mulq16', 'mulfr', 'clz'}


def _pipeline_multi(fn, L, skip):
    """Software-pipeline single-block hardware loop L over S >= 3 stages.

    Stages come from dependence levels where an edge from a computed value
    to a memory address, or from a load to its user (other than a stored
    value), starts a new stage. Memory accesses that may alias stay within
    one stage of each other in program order; a loop-carried value must be
    produced at most one stage after its first use. The steady-state block
    runs stage s of iteration j-s for every s, so its operations no longer
    wait on each other; values that live across stages travel through phis
    (induction pointers instead get adjusted memory offsets). Loops with
    fewer than S iterations run the original loop.
    """
    lc = L.term.imm
    exit_ = L.term.t[1]
    if exit_ is L: return False
    outside = [p for p in L.preds if p is not L]
    if len(outside) != 1: return False
    pre = outside[0]
    if pre.term.op != 'jmp' or pre.term.t[0] is not L: return False
    setlcs = [i for i in pre.ins if i.op == 'setlc' and i.imm == lc]
    if len(setlcs) != 1: return False
    setlc = setlcs[0]
    phis = list(L.phis)
    if any(len(p.t) != 2 for p in phis): return False
    body = [i for i in L.ins if i is not L.term]
    if any(i.op in ('call', 'setlc', 'halt', 'qcopy', 'div', 'udiv', 'mod', 'umod') or (i.op in ('load', 'loadq', 'store', 'storeq') and i.vol) for i in body):
        return False
    latch_of = {p.d: p.a[p.t.index(L)] for p in phis}
    init_of = {p.d: p.a[p.t.index(pre)] for p in phis}
    by_def = {i.d: i for i in body if i.d is not None}
    if any(v not in by_def for v in latch_of.values()): return False
    carried = set(latch_of.values())
    for b in fn.blocks:
        if b is L: continue
        for i in b.ins + b.phis:
            for x in i.a:
                if (x in by_def and x not in carried) or x in latch_of: return False
    consts = {i.d for i in body if i.op in ('const', 'sym')}
    dm = opt.def_map(fn)

    def cval(v):
        d = dm.get(v)
        return s32(d.imm) if d is not None and d.op == 'const' else None
    # Affine induction phis p' = p + c.
    iv = {}
    for p in phis:
        n = by_def.get(latch_of[p.d])
        if n is not None and n.op == 'add' and n.a[0] == p.d and cval(n.a[1]) is not None:
            iv[p.d] = cval(n.a[1])
    # Memory addresses p + c on an induction phi address p directly.
    for i in body:
        if i.op in ('load', 'store', 'loadq', 'storeq'):
            x = by_def.get(i.a[0])
            if x is not None and x.op == 'add' and x.a[0] in iv and cval(x.a[1]) is not None \
                    and (i.op in ('load', 'store') or cval(x.a[1]) % 4 == 0):
                i.a[0] = x.a[0]; i.imm = s32(i.imm + cval(x.a[1]))
    users = {}
    for i in body:
        for x in i.a: users.setdefault(x, []).append(i)
    addr_add = {i.d for i in body if i.op == 'add' and users.get(i.d)
                and all(u.op in ('load', 'store') and u.a[0] == i.d and i.d not in u.a[1:] for u in users[i.d])}
    comp = {}

    def is_compute(v, depth=0):
        if v in comp: return comp[v]
        i = by_def.get(v)
        r = False
        if i is not None and depth < 6:
            if i.op in _COMPUTE: r = True
            elif i.op in ('add', 'sub') and v not in addr_add:
                r = any(is_compute(x, depth+1) for x in i.a)
        comp[v] = r
        return r

    def weight(x, u, k):
        """Stage distance required between the producer of operand k (x) and u."""
        d = by_def.get(x)
        if d is None or d.op in ('const', 'sym'): return 0
        if d.op in ('load', 'loadq') and not (u.op in ('store', 'storeq') and k == 1): return 1
        if (u.op in ('load', 'store', 'loadq', 'storeq') and k == 0 or u.d in addr_add) and is_compute(x): return 1
        return 0
    stage = {}
    for i in body:
        stage[id(i)] = max((stage[id(by_def[x])] + weight(x, i, k) for k, x in enumerate(i.a) if x in by_def), default=0)
    if max(stage.values(), default=0) < 2: return False   # two stages: the older pipeliner
    # Constraints.
    alias = opt.AliasInfo(fn, dm, getattr(fn, 'options', None))
    mem = [i for i in body if i.op in ('load', 'store', 'loadq', 'storeq')]
    pairs = []
    for x in range(len(mem)):
        for y in range(x+1, len(mem)):
            a, b = mem[x], mem[y]
            if a.op in ('load', 'loadq') and b.op in ('load', 'loadq'): continue
            if alias.may_alias(alias.addr(a), alias.addr(b), cross=True): pairs.append((a, b))
    uses_of_phi = {p: [i for i in body if p in i.a] for p in latch_of}
    for _ in range(40):
        moved = False
        for i in body:
            s = max((stage[id(by_def[x])] + weight(x, i, k) for k, x in enumerate(i.a) if x in by_def), default=0)
            if s > stage[id(i)]: stage[id(i)] = s; moved = True
        for a, b in pairs:
            if stage[id(b)] < stage[id(a)]: stage[id(b)] = stage[id(a)]; moved = True
            if stage[id(b)] > stage[id(a)] + 1: stage[id(a)] = stage[id(b)] - 1; moved = True
        for p, n in latch_of.items():
            if n not in by_def: continue
            sn = stage[id(by_def[n])]
            for u in uses_of_phi[p]:
                if stage[id(u)] < sn - 1: stage[id(u)] = sn - 1; moved = True
        if not moved: break
    else:
        return False
    S = max(stage.values()) + 1
    if S < 3 or S > PIPE_MAX_STAGES: return False
    stage_of = {i.d: stage[id(i)] for i in body if i.d is not None}
    groups = [[i for i in body if stage[id(i)] == s and i.op not in ('const', 'sym')] for s in range(S)]
    # --- blocks -------------------------------------------------------------
    PRO = fn.block(); K = fn.block(); EPI = fn.block(); FB = fn.block(); J = fn.block()
    for b in (PRO, K, EPI, FB, J): b.depth = L.depth; b.freq = L.freq
    PRO.depth = EPI.depth = FB.depth = J.depth = pre.depth
    PRO.freq = EPI.freq = FB.freq = J.freq = pre.freq

    def clone_const(block, x, memo):
        if x not in memo:
            src = by_def[x]
            c = Ins(src.op, fn.vreg(), (), src.imm, src.s); c.blk = block; block.ins.append(c)
            memo[x] = c.d
        return memo[x]

    def emit(block, i, args, imm):
        c = Ins(i.op, fn.vreg() if i.d is not None else None, args, imm, i.s, [], i.vol); c.blk = block
        block.ins.append(c)
        return c.d
    # Prologue: blocks 0..S-2, stage s of iteration b-s.
    inst = {}
    memo = {}

    def pro_get(x, m):
        if x in consts: return clone_const(PRO, x, memo)
        if x in latch_of:
            return init_of[x] if m == 0 else pro_get(latch_of[x], m - 1)
        if x in by_def: return inst[(x, m)]
        return x
    for blk in range(S-1):
        for s in range(blk, -1, -1):
            m = blk - s
            for i in groups[s]:
                d = emit(PRO, i, [pro_get(x, m) for x in i.a], i.imm)
                if i.d is not None: inst[(i.d, m)] = d
    # Kernel: stage s of iteration j-s, later stages first.
    kmemo = {}
    kcur = {}
    kphis = {}

    def kphi(key):
        if key in kphis: return kphis[key]
        kind, x, a = key
        q = Ins('phi', fn.vreg(), []); q.blk = K; K.phis.append(q); kphis[key] = q.d
        if kind == 'v':
            entry = inst[(x, (S-1) - a - stage_of[x])]
        else:
            n = latch_of[x]
            m = (S-1) - a - stage_of[n]
            entry = init_of[x] if m < 0 else inst[(n, m)]
        back = kcur_or_phi(kind, x, a-1)
        q.a = [entry, back]; q.t = [PRO, K]
        return q.d

    def kcur_or_phi(kind, x, a):
        if a == 0: return ('late', kind, x)   # resolved after the kernel body exists
        return kphi((kind, x, a))

    def k_get(x, t, i, k):
        """Operand x of instruction i (position k) executing at stage t."""
        if x in consts: return clone_const(K, x, kmemo), 0
        if x in latch_of:
            n = latch_of[x]
            if n not in by_def: return x, 0
            sn = stage_of[n]
            a = t + 1 - sn
            if a == 0: return kcur[n], 0
            if x in iv and a >= 2 and k == 0 and i.op in ('load', 'store', 'loadq', 'storeq'):
                return kphi(('p', x, 1)), (sn - t) * iv[x]
            return kphi(('p', x, a)), 0
        if x in by_def:
            a = t - stage_of[x]
            if a == 0: return kcur[x], 0
            return kphi(('v', x, a)), 0
        return x, 0
    for s in range(S-1, -1, -1):
        for i in groups[s]:
            args = []; adj = 0
            for k, x in enumerate(i.a):
                v, off = k_get(x, s, i, k)
                args.append(v); adj += off
            d = emit(K, i, args, i.imm + adj)
            if i.d is not None: kcur[i.d] = d
    # Resolve age-0 back edges now that every kernel value exists.
    for q in K.phis:
        if q.a and isinstance(q.a[1], tuple):
            _, kind, x = q.a[1]
            q.a[1] = kcur[x] if kind == 'v' else kcur[latch_of[x]]
    # Epilogue: after the last kernel block (iteration N-1 = relative 0).
    einst = {}
    ememo = {}

    def e_get(x, r, t, i, k):
        """Operand x for relative iteration r executing at stage t."""
        if x in consts: return clone_const(EPI, x, ememo), 0
        if x in latch_of:
            n = latch_of[x]
            if n not in by_def: return x, 0
            if (n, r-1) in einst: return einst[(n, r-1)], 0
            a = -(r - 1 + stage_of[n])
            if x in iv and a >= 1 and k == 0 and i.op in ('load', 'store', 'loadq', 'storeq'):
                return kcur[n], (r - 1 + stage_of[n]) * iv[x]
            return (kcur[n] if a == 0 else kphi(('v', n, a))), 0
        if x in by_def:
            if (x, r) in einst: return einst[(x, r)], 0
            a = -(r + stage_of[x])
            return (kcur[x] if a == 0 else kphi(('v', x, a))), 0
        return x, 0
    for e in range(1, S):
        for s in range(S-1, e-1, -1):
            r = e - s
            for i in groups[s]:
                args = []; adj = 0
                for k, x in enumerate(i.a):
                    v, off = e_get(x, r, s, i, k)
                    args.append(v); adj += off
                d = emit(EPI, i, args, i.imm + adj)
                if i.d is not None: einst[(i.d, r)] = d
    # Kernel phis created while emitting the epilogue also need their back edges.
    changed_phis = True
    while changed_phis:
        changed_phis = False
        for q in list(K.phis):
            if q.a and isinstance(q.a[1], tuple):
                _, kind, x = q.a[1]
                q.a[1] = kcur[x] if kind == 'v' else kcur[latch_of[x]]
                changed_phis = True
    final = {}
    for p, n in latch_of.items():
        if n not in by_def: continue
        v, off = e_get(n, 0, S, by_def[n], -1) if (n, 0) not in einst else (einst[(n, 0)], 0)
        final[n] = v
    # --- wiring --------------------------------------------------------------
    n = setlc.a[0]
    pre.ins.remove(setlc)
    cS = Ins('const', fn.vreg(), (), S); cS.blk = pre
    pre.ins.insert(len(pre.ins)-1, cS)
    pre.ins[-1] = _mk(Ins('br', None, [n, cS.d], 0, 'geu', [PRO, FB]), pre)
    cK = Ins('const', fn.vreg(), (), S-1); cK.blk = PRO
    nk = Ins('sub', fn.vreg(), [n, cK.d]); nk.blk = PRO
    PRO.ins += [cK, nk, _mk(Ins('setlc', None, [nk.d], lc), PRO), _mk(Ins('jmp', t=[K]), PRO)]
    K.ins.append(_mk(Ins('lcbr', None, [], lc, None, [K, EPI]), K))
    EPI.ins.append(_mk(Ins('jmp', t=[J]), EPI))
    FB.ins += [_mk(Ins('setlc', None, [n], lc), FB), _mk(Ins('jmp', t=[L]), FB)]
    for p in phis: p.t = [FB if x is pre else x for x in p.t]
    L.term.t = [L, J]
    J.ins.append(_mk(Ins('jmp', t=[exit_]), J))
    for ph in exit_.phis: ph.t = [J if x is L else x for x in ph.t]
    mapping = {}
    for x, v in final.items():
        q = Ins('phi', fn.vreg(), [v, x]); q.t = [EPI, L]; q.blk = J; J.phis.append(q)
        mapping[x] = q.d
    for b in fn.blocks:
        if b in (L, K, PRO, EPI, J): continue
        for i in b.phis + b.ins:
            i.a = [mapping.get(x, x) for x in i.a]
    skip.add(L.id); skip.add(K.id)
    if not hasattr(fn, 'remainder_loops'): fn.remainder_loops = set()
    fn.remainder_loops.add(L.id)
    return True


def _pipeline(fn, L):
    lc = L.term.imm
    exit_ = L.term.t[1]
    if exit_ is L: return False
    outside = [p for p in L.preds if p is not L]
    if len(outside) != 1: return False
    pre = outside[0]
    if pre.term.op != 'jmp' or pre.term.t[0] is not L: return False
    setlc = [i for i in pre.ins if i.op == 'setlc' and i.imm == lc]
    if len(setlc) != 1: return False
    setlc = setlc[0]
    phis = list(L.phis)
    if any(len(p.t) != 2 for p in phis): return False
    body = [i for i in L.ins if i is not L.term]
    if any(i.op in ('call', 'setlc', 'halt', 'qcopy') or (i.op in ('load', 'loadq') and i.vol) or (i.op in ('store', 'storeq') and i.vol) for i in body):
        return False
    latch_of = {p.d: p.a[p.t.index(L)] for p in phis}
    init_of = {p.d: p.a[p.t.index(pre)] for p in phis}
    inside = {i.d for i in body if i.d is not None}
    # Stage 1: non-memory operations fed only by invariants, stage-1 values and
    # phis whose next value is itself computed in stage 1.
    # Loads may run one iteration ahead when no stage-2 store can alias them.
    alias = opt.AliasInfo(fn, opt.def_map(fn), getattr(fn, 'options', None))
    stores = [alias.addr(i) for i in body if i.op in ('store', 'storeq')]

    def early_load(i):
        if i.op not in ('load', 'loadq') or i.vol: return False
        ad = alias.addr(i)
        return not any(alias.may_alias(ad, st, cross=True) for st in stores)
    s1 = {id(i) for i in body if (i.op not in ('load', 'store', 'loadq', 'storeq') or early_load(i)) and i.d is not None}
    by_def = {i.d: i for i in body if i.d is not None}
    changed = True
    while changed:
        changed = False
        s1_vals = {i.d for i in body if id(i) in s1}
        s1_phis = {p for p, nxt in latch_of.items() if nxt in s1_vals or (nxt not in inside and nxt not in latch_of)}
        for i in body:
            if id(i) not in s1: continue
            ok = True
            for x in i.a:
                if x in inside and x not in s1_vals: ok = False; break
                if x in latch_of and x not in s1_phis: ok = False; break
            if not ok:
                s1.discard(id(i)); changed = True
    # Cheap offsets of carried/invariant values used only by stage 2 are
    # recomputed there instead of being carried through extra phis.
    consts = {i.d for i in body if i.op == 'const'}
    defs_all = opt.def_map(fn)
    moved = True
    while moved:
        moved = False
        for i in body:
            if id(i) not in s1 or i.op != 'add' or i.d is None: continue
            x, y = i.a
            ycon = y in consts or (defs_all.get(y) is not None and defs_all[y].op == 'const')
            if not ycon or x in inside: continue
            users = [u for u in body if i.d in u.a]
            if users and all(id(u) not in s1 for u in users) and i.d not in latch_of.values():
                s1.discard(id(i)); moved = True
        # An add used only as one stage-2 memory address folds into [Jx + Jy]
        # addressing there, so keep it in stage 2 rather than carrying its result.
        for i in body:
            if id(i) not in s1 or i.op != 'add' or i.d is None or i.d in latch_of.values(): continue
            users = [u for u in body if i.d in u.a]
            if len(users) == 1 and users[0].op in ('load', 'store') and users[0].a[0] == i.d                     and users[0].imm == 0 and id(users[0]) not in s1 and i.d not in users[0].a[1:]:
                s1.discard(id(i)); moved = True
    S1 = [i for i in body if id(i) in s1]
    S2 = [i for i in body if id(i) not in s1]
    # Constants used by stage 2 are cloned there too (they are free).
    s2_consts = [i for i in S1 if i.op == 'const' and any(i.d in u.a for u in S2)]
    S2 = s2_consts + S2
    # Worth it only when stage 1 holds real work (not just constants and increments).
    work = [i for i in S1 if i.op != 'const' and i.d not in latch_of.values()]
    if len(work) < 2 or not any(i.op in ('load', 'store', 'loadq', 'storeq') for i in S2): return False
    # Stage-2 memory ops addressing through a carried pointer p whose next value
    # p' = p + c is computed in stage 1 can use p' with the offset reduced by c,
    # so p itself need not survive into stage 2 (no extra copies).
    s1_ids = {id(i) for i in S1}
    for p_, n_ in latch_of.items():
        ni = by_def.get(n_)
        if ni is None or id(ni) not in s1_ids or ni.op != 'add' or ni.a[0] != p_: continue
        cdef = opt.def_map(fn).get(ni.a[1])
        if cdef is None or cdef.op != 'const': continue
        c = s32(cdef.imm)
        dm = opt.def_map(fn)
        for i in S2:
            if i.op not in ('load', 'store') or p_ in i.a[1:]: continue
            base = i.a[0]
            if base == p_:
                k = 0
            else:
                bd = dm.get(base)
                kd = dm.get(bd.a[1]) if bd is not None and bd.op == 'add' and bd.a[0] == p_ else None
                if kd is None or kd.op != 'const': continue
                k = s32(kd.imm)
            i.a[0] = n_; i.imm = i.imm + k - c
    s1_vals = [i.d for i in S1]
    s1_set = set(s1_vals)
    # Stage-1 values used by stage 2 (or as next values) travel through phis.
    needed = [v for v in s1_vals if any(v in i.a for i in S2) or v in latch_of.values()]
    # Values used after the loop must be carried values.
    carried = set(latch_of.values())
    for b in fn.blocks:
        if b is L: continue
        for i in b.ins + b.phis:
            for x in i.a:
                if (x in inside and x not in carried) or x in latch_of: return False

    def clone(block, ops, env, pos=None):
        out = []
        for i in ops:
            c = Ins(i.op, fn.vreg() if i.d is not None else None, [env.get(x, x) for x in i.a], i.imm, i.s, [], i.vol)
            if i.d is not None: env[i.d] = c.d
            c.blk = block; out.append(c)
        if pos is None: block.ins.extend(out)
        else: block.ins[pos:pos] = out
        return env

    # Prologue: stage 1 of iteration 0 in the preheader, before setlc.
    env0 = dict(init_of)
    pos = pre.ins.index(setlc)
    clone(pre, S1, env0, pos)
    # Trip count N-1 for the steady state; N == 1 skips straight to the epilogue.
    n = setlc.a[0]
    one = Ins('const', fn.vreg(), (), 1); one.blk = pre
    n1 = Ins('sub', fn.vreg(), [n, one.d]); n1.blk = pre
    zero = Ins('const', fn.vreg(), (), 0); zero.blk = pre
    k = pre.ins.index(setlc)
    pre.ins[k:k] = [one, n1, zero]
    setlc.a = [n1.d]
    epi = fn.block(); steady = fn.block()
    pre.ins[-1] = _mk(Ins('br', None, [n1.d, zero.d], 0, 'eq', [epi, steady]), pre)
    # Steady-state loop: stage 2 of iteration i, then stage 1 of iteration i+1.
    lp_phi = {}
    for p in phis:
        q = Ins('phi', fn.vreg(), [init_of[p.d]]); q.t = [pre]; q.blk = steady
        steady.phis.append(q); lp_phi[p.d] = q
    sv_phi = {}
    for v in needed:
        q = Ins('phi', fn.vreg(), [env0[v]]); q.t = [pre]; q.blk = steady
        steady.phis.append(q); sv_phi[v] = q
    env = {p: q.d for p, q in lp_phi.items()}
    env.update({v: q.d for v, q in sv_phi.items()})
    clone(steady, S2, env)
    nxt = {p: env.get(v, v) for p, v in latch_of.items()}
    env1 = dict(nxt)
    clone(steady, S1, env1)
    for p, q in lp_phi.items(): q.t.append(steady); q.a.append(nxt[p])
    for v, q in sv_phi.items(): q.t.append(steady); q.a.append(env1[v])
    steady.ins.append(_mk(Ins('lcbr', None, [], lc, None, [steady, epi]), steady))
    # Epilogue: stage 2 of the last iteration.
    ep_phi = {}
    for p in phis:
        q = Ins('phi', fn.vreg(), [init_of[p.d], nxt[p.d]]); q.t = [pre, steady]; q.blk = epi
        epi.phis.append(q); ep_phi[p.d] = q.d
    ep_sv = {}
    for v in needed:
        q = Ins('phi', fn.vreg(), [env0[v], env1[v]]); q.t = [pre, steady]; q.blk = epi
        epi.phis.append(q); ep_sv[v] = q.d
    env2 = dict(ep_phi); env2.update(ep_sv)
    clone(epi, S2, env2)
    final = {p: env2.get(v, v) for p, v in latch_of.items()}
    epi.ins.append(_mk(Ins('jmp', t=[exit_]), epi))
    mapping = {latch_of[p]: final[p] for p in latch_of}
    for b in fn.blocks:
        if b in (L, steady, epi): continue
        for i in b.phis:
            i.t = [epi if x is L else x for x in i.t]
            i.a = [mapping.get(x, x) for x in i.a]
        for i in b.ins:
            i.a = [mapping.get(x, x) for x in i.a]
    fn.blocks.remove(L)
    return True


def _break_add_chains(fn, M, pre):
    """In an unrolled body, `v1 = v0 + s; v2 = v1 + s; ...` with s invariant
    becomes `vk = v0 + k*s` (k*s formed in the preheader): the copies no longer
    wait for each other."""
    local = {i.d: i for i in M.ins if i.d is not None}
    defs = opt.def_map(fn)
    scaled = {}

    def multiple(s, k):
        key = (s, k)
        if key in scaled: return scaled[key]
        d = defs.get(s)
        # At the top: later passes insert code that uses these before `setlc'.
        pos = 0
        if d is not None and d.op == 'const':
            n = Ins('const', fn.vreg(), (), (d.imm * k) & MASK)
        else:
            c = Ins('const', fn.vreg(), (), k); c.blk = pre; pre.ins.insert(pos, c); pos += 1
            n = Ins('mul', fn.vreg(), [s, c.d])
        n.blk = pre; pre.ins.insert(pos, n)
        scaled[key] = n.d
        return n.d
    orig = {i.d: tuple(i.a) for i in M.ins if i.op == 'add' and len(i.a) == 2}
    for i in M.ins:
        if i.op != 'add' or len(i.a) != 2: continue
        x, s = orig[i.d]
        if s in local or s == x: continue
        # Walk back through the chain x = y + s, y = z + s, ...
        k = 1; base = x
        while base in orig and orig[base][1] == s and orig[base][0] != s:
            base = orig[base][0]; k += 1
        if k == 1: continue
        i.a = [base, multiple(s, k)]


def _unrollable(fn, lp, latch, exit_):
    """Only loop-carried (phi-feeding) values may be used after the loop."""
    inside = {i.d for i in latch.ins if i.d is not None} | {p.d for p in latch.phis}
    carried = {p.a[p.t.index(latch)] for p in latch.phis}
    phis = {p.d for p in latch.phis}
    for b in fn.blocks:
        if b is latch: continue
        for i in b.phis:
            for pred, x in zip(i.t, i.a):
                if x in inside and x not in carried: return False
                if x in phis: return False
        for i in b.ins:
            for x in i.a:
                if x in inside and x not in carried: return False
                if x in phis: return False
    return True


def _unroll(fn, lp, L, exit_, n, lc, U, defs):
    """Remainder loop (N mod U) followed by an unrolled main loop (N / U)."""
    pre = lp.preheader
    shift = U.bit_length() - 1
    phis = list(L.phis)
    body = [i for i in L.ins if i is not L.term]
    carried = [p.a[p.t.index(L)] for p in phis]
    starts = [p.a[1 - p.t.index(L)] for p in phis]

    def clone(block, in_vals):
        """Append a copy of the body to `block`; phi values bound to in_vals. Returns next values."""
        vmap = {p.d: v for p, v in zip(phis, in_vals)}
        memo = {}
        for i in body:
            c = Ins(i.op, fn.vreg() if i.d is not None else None, [vmap.get(x, x) for x in i.a], i.imm, _retag(i.s, memo), [], i.vol)
            if i.d is not None: vmap[i.d] = c.d
            c.blk = block; block.ins.append(c)
        return [vmap.get(x, x) for x in carried]

    def new_phis(block, preds_vals):
        out = []
        for k in range(len(phis)):
            p = Ins('phi', fn.vreg(), [vals[k] for _, vals in preds_vals]); p.t = [b for b, _ in preds_vals]; p.blk = block
            block.phis.append(p); out.append(p.d)
        return out

    def const_in(block, v):
        c = Ins('const', fn.vreg(), (), v & MASK); c.blk = block
        block.ins.insert(len(block.ins)-1 if block.term else len(block.ins), c)
        return c.d

    # Preheader: r = ((N-1) & (U-1)) + 1 in 1..U, q = (N - r) / U. The remainder
    # loop always runs, so N = 0 (2^32 iterations) stays exact.
    pre.ins.pop()
    one = const_in(pre, 1); mask = const_in(pre, U-1); sh = const_in(pre, shift)
    def op(o, a):
        i = Ins(o, fn.vreg(), a); i.blk = pre; pre.ins.append(i); return i.d
    r = op('add', [op('and', [op('sub', [n, one]), mask]), one])
    q = op('shr', [op('sub', [n, r]), sh])
    R = fn.block(); mid = fn.block(); main_pre = fn.block(); M = fn.block(); after = fn.block()
    pre.ins.append(_mk(Ins('setlc', None, [r], lc), pre))
    pre.ins.append(_mk(Ins('jmp', t=[R]), pre))
    # Remainder loop.
    rp = new_phis(R, [(pre, starts)])
    rn = clone(R, rp)
    for p, v in zip(R.phis, rn): p.t.append(R); p.a.append(v)
    R.ins.append(_mk(Ins('lcbr', None, [], lc, None, [R, mid]), R))
    if not hasattr(fn, 'remainder_loops'): fn.remainder_loops = set()
    fn.remainder_loops.add(R.id)
    # Main loop when N >= U.
    zero = const_in(mid, 0)
    mid.ins.append(_mk(Ins('br', None, [q, zero], 0, 'eq', [after, main_pre]), mid))
    main_pre.ins.append(_mk(Ins('setlc', None, [q], lc), main_pre))
    main_pre.ins.append(_mk(Ins('jmp', t=[M]), main_pre))
    mp = new_phis(M, [(main_pre, rn)])
    vals = mp
    for _ in range(U): vals = clone(M, vals)
    for p, v in zip(M.phis, vals): p.t.append(M); p.a.append(v)
    M.ins.append(_mk(Ins('lcbr', None, [], lc, None, [M, after]), M))
    final = new_phis(after, [(mid, rn), (M, vals)])
    after.ins.append(_mk(Ins('jmp', t=[exit_]), after))
    _break_add_chains(fn, M, main_pre)
    # Uses after the loop referred to carried values from L: map them to the final values.
    mapping = {c: f for c, f in zip(carried, final)}
    for b in fn.blocks:
        if b in (L, R, M, mid, after, main_pre): continue
        for i in b.phis:
            i.a = [mapping.get(x, x) if pred is not L else mapping.get(x, x) for pred, x in zip(i.t, i.a)]
            i.t = [after if pred is L else pred for pred in i.t]
        for i in b.ins:
            i.a = [mapping.get(x, x) for x in i.a]
    # Detach the original loop block.
    fn.blocks.remove(L)
