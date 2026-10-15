"""Inlining of same-translation-unit functions at the IR level (-O2).

Callees are optimized first (bottom-up over the call graph), then their SSA
bodies are cloned into call sites of the not-yet-optimized caller. Arguments
replace the callee's parameter loads, so no stack traffic remains. Static
functions left without references are not emitted.
"""
from __future__ import annotations
import re
from . import ir, opt
from .ir import Ins

SMALL = 40          # always inline callees up to this many IR instructions
TINY = 8            # ... but only these into cold blocks (paths that cannot return)
COLD = 1e-2         # block frequency (relative to loop depth 0) of such blocks
SINGLE_USE = 400    # static callees with one call site
IN_LOOP = 320       # callees called from inside a loop
CALLER_LIMIT = 6000
MAX_SITES = 64       # inlined call sites per caller


def size(fn):
    return sum(len(b.ins) + len(b.phis) for b in fn.blocks)


def sccs(names, edges):
    index = {}; low = {}; stack = []; on = set(); out = []; counter = [0]

    def visit(v):
        work = [(v, iter(edges.get(v, ())))]
        index[v] = low[v] = counter[0]; counter[0] += 1; stack.append(v); on.add(v)
        while work:
            u, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1; stack.append(w); on.add(w)
                    work.append((w, iter(edges.get(w, ()))))
                    break
                elif w in on:
                    low[u] = min(low[u], index[w])
            else:
                work.pop()
                if work: low[work[-1][0]] = min(low[work[-1][0]], low[u])
                if low[u] == index[u]:
                    comp = []
                    while True:
                        w = stack.pop(); on.discard(w); comp.append(w)
                        if w == u: break
                    out.append(comp)
    for v in names:
        if v not in index: visit(v)
    return out   # reverse topological: callees before callers


def inline_functions(compiler, functions, options):
    by_symbol = {f.symbol: f for f in functions}
    edges = {f.symbol: {s for s in f.calls if s in by_symbol} for f in functions}
    order = sccs([f.symbol for f in functions], edges)
    comp_of = {}
    for k, comp in enumerate(order):
        for s in comp: comp_of[s] = k
    # Call-site counts and address uses (sym references) of each function.
    sites = {}; referenced = set()
    for f in functions:
        for b in f.blocks:
            for i in b.ins:
                if i.op == 'call' and i.s in by_symbol: sites[i.s] = sites.get(i.s, 0) + 1
                if i.op == 'sym' and i.s in by_symbol: referenced.add(i.s)
    # Data initializers and code emitted outside the IR (-O0 fallbacks) can reference functions.
    data_text = '\n'.join(compiler.data) + '\n' + '\n'.join(compiler.code)
    for s in by_symbol:
        if re.search(r'(?<![A-Za-z_0-9])' + re.escape(s) + r'(?![A-Za-z_0-9])', data_text): referenced.add(s)
    optimized = {}
    level = compiler.optimize
    for comp in order:
        for sym in comp:
            fn = by_symbol[sym]
            _inline_into(fn, by_symbol, optimized, comp_of, sites, referenced, getattr(compiler, 'noinline', ()))
            opt.optimize(fn, options, level)
            fn.optimized = True
            optimized[sym] = fn
    # Drop static functions that are no longer called or referenced.
    live_calls = {}
    for f in functions:
        for b in f.blocks:
            for i in b.ins:
                if i.op == 'call' and i.s: live_calls[i.s] = live_calls.get(i.s, 0) + 1
                if i.op == 'sym' and i.s in by_symbol: referenced.add(i.s)
    keep = []
    for f in functions:
        if f.static and f.symbol not in referenced and not live_calls.get(f.symbol):
            continue
        keep.append(f)
    return keep


def _eligible(caller, callee, call, comp_of, sites, referenced, depth=0, freq=1.0, noinline=()):
    if callee is caller or comp_of[callee.symbol] == comp_of[caller.symbol]: return False
    if callee.variadic or callee.name in noinline: return False
    nargs = len(call.a)
    if nargs != callee.nparams: return False
    n = size(callee)
    if freq < COLD and n > TINY: return False   # keep cold paths short
    if n <= SMALL: return True
    if depth >= 1 and n <= IN_LOOP: return True
    if callee.static and sites.get(callee.symbol) == 1 and callee.symbol not in referenced and n <= SINGLE_USE:
        return True
    return False


def _inline_into(fn, by_symbol, optimized, comp_of, sites, referenced, noinline=()):
    """Inline eligible call sites hottest first (block frequency, then program
    order) until the caller's size budget or MAX_SITES is used up. Each
    inlined site changes the block list, so the candidates are rescanned."""
    budget = CALLER_LIMIT - size(fn)
    for _ in range(MAX_SITES):
        ir.link(fn)
        ir.loops(fn)   # loop depth and frequency of each call site
        best = None
        for bn, b in enumerate(fn.blocks):
            for k, i in enumerate(b.ins):
                if i.op != 'call' or i.s not in optimized: continue
                callee = optimized[i.s]
                if size(callee) > budget: continue
                if not _eligible(fn, callee, i, comp_of, sites, referenced, b.depth, b.freq, noinline): continue
                key = (-b.freq, bn, k)
                if best is None or key < best[0]: best = (key, b, k, callee)
        if best is None: break
        _, b, k, callee = best
        budget -= size(callee)
        _inline_site(fn, b, k, callee)
    ir.link(fn)


_site_serial = [0]


def _inline_site(fn, b, k, callee):
    call = b.ins[k]
    cont = fn.block()
    cont.ins = b.ins[k+1:]
    for x in cont.ins: x.blk = cont
    b.ins = b.ins[:k]
    # Successor phis now see `cont` as predecessor.
    t = cont.term
    for s in t.t:
        for p in s.phis:
            p.t = [cont if x is b else x for x in p.t]
    vmap = {}

    def V(x):
        n = vmap.get(x)
        if n is None: n = vmap[x] = fn.vreg()
        return n
    bmap = {cb.id: fn.block() for cb in callee.blocks}
    # Parameters: promoted ones are loaded once from their argument slot.
    arg_objs = {f.id: f for f in callee.frame if f.kind == 'arg'}
    fmap = {}
    escaping = set()
    uses = opt.use_map(callee)
    for cb in callee.blocks:
        for ci in cb.ins:
            if ci.op == 'frame' and ci.imm in arg_objs:
                for u in uses.get(ci.d, ()):
                    if not (u.op == 'load' and u.a[0] == ci.d and u.imm == 0):
                        escaping.add(ci.imm)
    for f in callee.frame:
        if f.kind == 'arg' and f.id not in escaping: continue
        nf = fn.frame_object(f.size, 'local' if f.kind == 'arg' else f.kind)
        fmap[f.id] = nf.id
    args = list(call.a)
    for f in callee.frame:
        if f.kind == 'arg' and f.id in escaping:
            addr = fn.vreg()
            b.ins.append(Ins('frame', addr, (), fmap[f.id]))
            b.ins.append(Ins('store', None, [addr, args[f.index]], 0))
    param_frame = {}   # callee frame vreg -> argument index for promoted params
    for cb in callee.blocks:
        for ci in cb.ins:
            if ci.op == 'frame' and ci.imm in arg_objs and ci.imm not in escaping:
                param_frame[ci.d] = arg_objs[ci.imm].index
    rets = []
    tags = {}   # restrict:<id> alias tags describe one call: fresh ids per inlined copy

    def tag(s):
        if not (isinstance(s, str) and s.startswith('restrict:')): return s
        p, part = s.rsplit(':', 1)
        if p not in tags:
            _site_serial[0] += 1
            tags[p] = f'{p}.i{_site_serial[0]}'
        return tags[p] + ':' + part
    for cb in callee.blocks:
        nb = bmap[cb.id]
        for p in cb.phis:
            np_ = Ins('phi', V(p.d), [V(x) for x in p.a])
            np_.t = [bmap[x.id] for x in p.t]; np_.blk = nb
            nb.phis.append(np_)
        for ci in cb.ins:
            op = ci.op
            if op == 'frame' and ci.d in param_frame:
                continue
            if op == 'load' and ci.a[0] in param_frame and ci.imm == 0:
                nb.ins.append(Ins('copy', V(ci.d), [args[param_frame[ci.a[0]]]]))
                continue
            if op == 'ret':
                rets.append((nb, V(ci.a[0]) if ci.a else None))
                nb.ins.append(Ins('jmp', t=[cont]))
                continue
            n = Ins(op, V(ci.d) if ci.d is not None else None, [V(x) for x in ci.a], ci.imm, tag(ci.s),
                    [bmap[x.id] for x in ci.t], ci.vol)
            if isinstance(ci.s, list): n.s = list(ci.s)
            if op == 'frame': n.imm = fmap[ci.imm]
            nb.ins.append(n)
    b.ins.append(Ins('jmp', t=[bmap[callee.entry.id]]))
    if call.d is not None:
        vals = [(blk, v) for blk, v in rets if v is not None]
        if len(vals) == 1:
            cont.ins.insert(0, Ins('copy', call.d, [vals[0][1]]))
        elif vals:
            p = Ins('phi', call.d, [v for _, v in vals]); p.t = [blk for blk, _ in vals]; p.blk = cont
            cont.phis.append(p)
        else:
            cont.ins.insert(0, Ins('const', call.d, (), 0))
    fn.calls |= callee.calls
    ir.link(fn)
