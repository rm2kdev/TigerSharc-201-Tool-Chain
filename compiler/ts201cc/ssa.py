"""SSA construction (Cytron et al.) and destruction with parallel copies."""
from __future__ import annotations
from . import ir
from .ir import Ins


def build(fn):
    """Rename multiply-assigned variables into SSA form."""
    ir.link(fn)
    order = ir.dominators(fn)
    defs = {}
    count = {}
    for b in order:
        for i in b.ins:
            if i.d is not None:
                count[i.d] = count.get(i.d, 0) + 1
                defs.setdefault(i.d, set()).add(b)
    variables = {v for v, n in count.items() if n > 1} | (fn.variables & count.keys())
    # Variables used without any definition (uninitialised reads) still need renaming.
    for b in order:
        for i in b.ins:
            for x in i.a:
                if x in fn.variables and x not in count: variables.add(x)
    df = ir.frontiers(order)
    phis_for = {}
    for v in variables:
        work = list(defs.get(v, ()))
        placed = set(); seen = {b.id for b in work}
        while work:
            b = work.pop()
            for y in df[b.id]:
                if y.id in placed: continue
                placed.add(y.id)
                phi = Ins('phi', v, [v]*len(y.preds))
                phi.t = list(y.preds); phi.blk = y
                y.phis.append(phi)
                if y.id not in seen:
                    seen.add(y.id); work.append(y)
    stacks = {v: [] for v in variables}
    undef = {}

    def top(v):
        s = stacks.get(v)
        if s is None: return v
        if s: return s[-1]
        u = undef.get(v)
        if u is None: u = undef[v] = fn.vreg()   # uninitialised read: defined as 0 below
        return u

    pushed = {}
    work = [(fn.entry, False)]
    while work:
        b, done = work.pop()
        if done:
            for v in pushed.pop(b.id): stacks[v].pop()
            continue
        mine = []
        for p in b.phis:
            v = p.d
            if v in stacks:
                n = fn.vreg(); stacks[v].append(n); mine.append(v); p.d = n
        for i in b.ins:
            i.a = [top(x) if x in stacks else x for x in i.a]
            if i.d is not None and i.d in stacks:
                n = fn.vreg(); stacks[i.d].append(n); mine.append(i.d); i.d = n
        for s in b.succs:
            for p in s.phis:
                for k, pred in enumerate(p.t):
                    if pred is b and p.a[k] in stacks:
                        p.a[k] = top(p.a[k])
        pushed[b.id] = mine
        work.append((b, True))
        for c in reversed(b.dom_children): work.append((c, False))
    for u in undef.values():
        c = Ins('const', u, (), 0); c.blk = fn.entry
        fn.entry.ins.insert(0, c)
    fn.variables = set()


def sequentialize(copies, fresh):
    """Order a parallel copy [(dest, src)] into sequential copies."""
    copies = [(d, s) for d, s in copies if d != s]
    result = []
    pending = dict(copies)            # dest -> src
    uses = {}
    for d, s in copies: uses[s] = uses.get(s, 0) + 1
    ready = [d for d in pending if uses.get(d, 0) == 0]
    while pending:
        while ready:
            d = ready.pop()
            if d not in pending: continue
            s = pending.pop(d)
            result.append((d, s))
            uses[s] -= 1
            if uses[s] == 0 and s in pending: ready.append(s)
        if pending:
            # Break a cycle through a temporary.
            d, s = next(iter(pending.items()))
            t = fresh(d)
            result.append((t, d))
            for k, v in list(pending.items()):
                if v == d: pending[k] = t
            uses[t] = uses.pop(d, 0)
            uses[d] = 0
            ready.append(d)
    return result


def destroy(fn, fresh=None, live_out=None):
    """Replace phis by copies on incoming edges (splitting critical edges when needed)."""
    if fresh is None:
        fresh = lambda like: fn.vreg()
    placed = {}   # pred id -> dests of copies already placed before its terminator
    split_live = {}   # edge blocks created here -> their live-in set
    for b in list(fn.blocks):
        if not b.phis: continue
        for pi, pred in enumerate(list(b.preds)):
            copies = []
            for p in b.phis:
                k = p.t.index(pred)
                copies.append((p.d, p.a[k]))
            if not copies: continue
            target = pred
            t = pred.term
            dests = {d for d, _ in copies}
            safe = len(pred.succs) == 1
            # Only the hot edge (e.g. a loop back edge) keeps its copies in pred;
            # colder edges get their own block, which runs once.
            if not safe and t.op in ('br', 'lcbr') and live_out is not None and pred.id not in placed                     and b.freq >= pred.freq:
                # Copies may stay before the branch when the branch does not read
                # a destination and no destination reaches another successor
                # (live into it, or read by its phis along this edge).
                others = [s for s in pred.succs if s is not b]
                def reaches(s):
                    live = live_out(s) if s.id not in split_live else split_live[s.id]
                    if live is None: return True
                    ops = {p.a[p.t.index(pred)] for p in s.phis if pred in p.t}
                    return bool(dests & (live | ops))
                safe = not (dests & set(t.a)) and not any(reaches(s) for s in others)
                if safe and any(s.phis for s in others):
                    # Copies for the other successor's phis will then need their own edge block.
                    placed[pred.id] = dests
            elif not safe and pred.id in placed:
                safe = False
            if not safe:
                target = ir.split_edge(fn, pred, b)
                # Live into the new edge block: the copy sources plus what b needs.
                base_live = live_out(b) if live_out is not None else None
                split_live[target.id] = None if base_live is None else (set(base_live) | {s_ for _, s_ in copies})
            seq = sequentialize(copies, fresh)
            pos = len(target.ins) - 1
            for d, s in seq:
                c = Ins('copy', d, [s]); c.blk = target
                target.ins.insert(pos, c); pos += 1
        b.phis = []
    ir.link(fn)
