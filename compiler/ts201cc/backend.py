"""Optimizing backend driver: IR optimization, selection, allocation, scheduling.

Frame layout (J27 = stack pointer after the prologue, four-word aligned):
  [J27 + 0 .. A-1]   outgoing argument area (largest call, rounded to 4)
  [J27 + A]          saved CJMP (non-leaf functions)
  ...                callee-saved registers, spill slots, local objects
  [J27 + F + i]      incoming argument i (F = frame size)
J26 is an ordinary callee-saved register; generated code does not keep a
frame-pointer chain. The ABI contract (stack arguments, XR0 result,
callee-saved sets, J27 alignment) is unchanged.
"""
from __future__ import annotations
from functools import lru_cache
import re
from . import ir, opt, ssa, isel, regalloc
from .ir import MASK, s32
from .isel import PRIVATE
from .isel import MI

SEQ = {'br', 'jmp', 'call', 'calli', 'ret', 'ijmp', 'cset', 'cmov', 'idle', 'tail', 'cret'}
TERM = {'br', 'jmp', 'ret', 'ijmp', 'idle', 'tail', 'cret'}
# Intra-function JUMPs use the one-slot 15-bit PC-relative form `(S)` (no
# immediate extension) unless the function might exceed that range.
_SHORT = [True]
SHORT_LIMIT = 0x3c00
CALLEE_SAVED = [r for c in ('X', 'J', 'K', 'Y') for r in regalloc.REGS[c] if r not in regalloc.CALLER_SAVED]


def generate(compiler, functions):
    options = getattr(compiler, 'options', {}) or {}
    level = compiler.optimize
    if level >= 2 and not options.get('no_inline') and 'inline' not in opt.DISABLED:
        from .inline import inline_functions
        functions = inline_functions(compiler, functions, options)
    lines = []
    for fn in functions:
        if not getattr(fn, 'optimized', False): opt.optimize(fn, options, level)
        lines += Codegen(compiler, fn, options).run()
    return lines


# ---------------------------------------------------------------------------

def ir_liveness(fn):
    """live_in sets for SSA IR with phi semantics."""
    uses = {}; defs = {}
    for b in fn.blocks:
        u = set(); d = set(p.d for p in b.phis)
        for i in b.ins:
            for x in i.a:
                if x not in d: u.add(x)
            if i.d is not None: d.add(i.d)
        uses[b.id] = u; defs[b.id] = d
    live_in = {b.id: set() for b in fn.blocks}
    changed = True
    order = list(reversed(fn.blocks))
    while changed:
        changed = False
        for b in order:
            out = set()
            for s in b.succs:
                out |= live_in[s.id]
                for p in s.phis:
                    for pred, x in zip(p.t, p.a):
                        if pred is b: out.add(x)
            new = uses[b.id] | (out - defs[b.id])
            if new != live_in[b.id]:
                live_in[b.id] = new; changed = True
    return live_in


class Codegen:
    uid = 0

    def __init__(self, compiler, fn, options):
        self.c = compiler; self.fn = fn; self.options = options

    def run(self):
        fn = self.fn
        fn.options = self.options
        ir.link(fn)
        ir.loops(fn)
        defs = opt.def_map(fn)
        alias = opt.AliasInfo(fn, defs, self.options)
        self.alias = alias
        addr_of = {}
        for b in fn.blocks:
            for i in b.ins:
                if i.op in ('load', 'store', 'loadq', 'storeq'): addr_of[id(i)] = alias.addr(i)
        from . import loops
        if loops.transform(fn, self.options, unroll=self.c.optimize >= 2 and 'unroll' not in opt.DISABLED):
            opt.instcombine(fn)
            if 'thread' not in opt.DISABLED and opt.thread_phi_branches(fn): opt.instcombine(fn)
            defs = opt.def_map(fn)
            opt.gvn(fn, opt.AliasInfo(fn, defs, self.options))
            opt.dce(fn)
            if 'licm2' not in opt.DISABLED and opt.licm(fn, opt.AliasInfo(fn, opt.def_map(fn), self.options)):
                opt.instcombine(fn)
                opt.gvn(fn, opt.AliasInfo(fn, opt.def_map(fn), self.options))
                opt.dce(fn)
            ir.link(fn); ir.loops(fn)
            defs = opt.def_map(fn)
            alias = opt.AliasInfo(fn, defs, self.options)
            self.alias = alias
            addr_of = {}
            for b in fn.blocks:
                for i in b.ins:
                    if i.op in ('load', 'store', 'loadq', 'storeq'): addr_of[id(i)] = alias.addr(i)
        if 'rebase' not in opt.DISABLED and isel.rebase_offsets(fn):
            opt.dce(fn)
            defs = opt.def_map(fn)
        cls = isel.assign_classes(fn, defs)
        cls = isel.split_ialu(fn, defs, cls)
        if isel.split_compute(fn, defs, cls):
            defs = opt.def_map(fn)
        if 'clip' not in opt.DISABLED and isel.form_clip(fn, cls):
            opt.dce(fn)
            defs = opt.def_map(fn)
        if 'fext' not in opt.DISABLED and isel.form_fext(fn, cls):
            opt.dce(fn)
            defs = opt.def_map(fn)
        if 'balance' not in opt.DISABLED and isel.balance_units(fn, cls):
            defs = opt.def_map(fn)
        isel.fixup(fn, defs, cls)
        defs = opt.def_map(fn)
        live_in = ir_liveness(fn)
        ssa.destroy(fn, fresh=lambda like: self._fresh(cls, like), live_out=lambda s: live_in.get(s.id))
        ir.loops(fn)
        mf = isel.select(fn, defs, cls, addr_of)
        self.mf = mf
        if 'mcleanup' not in opt.DISABLED: cleanup(mf)
        self._rename = 'rename' not in opt.DISABLED
        if 'prera' not in opt.DISABLED:
            global _ALIAS
            _ALIAS = self.alias
            prera_schedule(mf)
        regalloc.allocate(mf)
        if 'postinc' not in opt.DISABLED: post_ra(mf)
        if self._rename: rename_registers(mf)
        self.frame(mf)
        return self.emit(mf)

    def _fresh(self, cls, like):
        self.fn.nv += 1
        cls[self.fn.nv] = cls.get(like, 'X')
        return self.fn.nv

    # -- frame --------------------------------------------------------------
    def tail_calls(self, mf):
        """Turn `CALL f; return f's result` into a jump to f after this frame is released.

        The callee's stack arguments go to this function's incoming argument
        slots, which the caller allocated (round_up(nparams, 4) words), so the
        callee finds them at the same J27 and returns straight to our caller
        through the restored CJMP. Needs: no frame-object address escapes (the
        frame is gone when the callee runs) and no incoming argument read after
        its slot is overwritten."""
        if 'tail' in opt.DISABLED: return
        for b in mf.blocks:
            for mi in b.mis:
                e = mi.extra
                if mi.op not in ('ld', 'st') and isinstance(e, tuple) and e and e[0] == 'frame': return
                if mi.op == 'macro': return
        room = (self.fn.nparams + 3) & -4
        converted = False
        for b in mf.blocks:
            mis = b.mis
            if len(mis) < 2 or mis[-1].op != 'ret' or mis[-2].op != 'call' or mis[-2].vol: continue
            k = len(mis) - 2
            start = k
            while start > 0 and mis[start-1].op not in ('call', 'calli', 'macro'): start -= 1
            stores = [mi for mi in mis[start:k] if mi.op == 'st' and isinstance(mi.extra, tuple) and mi.extra[0] == 'arg']
            nargs = max((mi.extra[1] + 1 for mi in stores), default=0)
            if nargs > room: continue
            first = next((j for j in range(start, k) if mis[j] in stores), k)
            clash = False
            for mi in mis[first:k]:
                e = mi.extra
                if mi.op == 'ld' and isinstance(e, tuple) and e[0] == 'frame':
                    obj = mf.frame[e[1]]
                    if obj.kind == 'arg' and obj.index < nargs: clash = True
            if clash: continue
            for mi in stores: mi.extra = ('tailarg', mi.extra[1])
            call = mis[k]
            b.mis[k:] = [MI('tail', [], [], None, call.s)]
            converted = True
        if not converted: return
        # Released outgoing area and CJMP save of calls that became jumps.
        args = 0; leaf = True
        for b in mf.blocks:
            for mi in b.mis:
                if mi.op == 'st' and isinstance(mi.extra, tuple) and mi.extra[0] == 'arg':
                    args = max(args, (mi.extra[1] + 4) & -4)
                if mi.op in ('call', 'calli', 'ijmp') or mi.extra == 'cjmp': leaf = False
        mf.max_args = args
        mf.leaf = mf.leaf or leaf

    def frame(self, mf):
        self.tail_calls(mf)
        saves = [r for r in CALLEE_SAVED if r in mf.used_regs]
        off = mf.max_args
        cj = None
        if not mf.leaf:
            cj = off; off += 1
        slots = {}
        # Quad-aligned groups of callee-saved X/J/K registers save four words at once.
        groups = []
        for prefix, regs in (('XR', regalloc.XREGS), ('YR', regalloc.YREGS), ('J', [f'J{i}' for i in range(32)]), ('K', [f'K{i}' for i in range(32)])):
            used = [r for r in saves if r.startswith(prefix) and (prefix != 'J' or not r.startswith('JB'))]
            used = [r for r in used if (r.startswith(prefix) if prefix in ('XR', 'YR') else not r[1:2] == 'R')]
            quads = {}
            for r in used:
                n = int(r[len(prefix):])
                quads.setdefault(n//4, []).append(n)
            for q, ns in sorted(quads.items()):
                groups.append((prefix, q, sorted(ns)))
        # Word-aligned quad slots after the CJMP slot.
        off = (off + 3) & -4 if groups else off
        save_plan = []
        for prefix, q, ns in groups:
            base = off; off += 4
            save_plan.append((prefix, q, ns, base))
        for obj in mf.frame:
            if obj.kind == 'arg': continue
            obj.offset = off; off += obj.size
        F = (off + 3) & -4
        for obj in mf.frame:
            if obj.kind == 'arg': obj.offset = F + obj.index
        self.F = F; self.cj = cj; self.save_plan = save_plan
        # Frame objects whose address is formed (and may reach a pointer).
        escaped = {mi.extra[1] for b in mf.blocks for mi in b.mis
                   if mi.op not in ('ld', 'st') and isinstance(mi.extra, tuple) and mi.extra and mi.extra[0] == 'frame'}
        # Resolve frame-relative immediates. Spills, outgoing and tail-call
        # arguments and loads of never-addressed frame slots are PRIVATE: no
        # pointer reaches them (the scheduler compares their SP offsets only).
        for b in mf.blocks:
            for mi in b.mis:
                e = mi.extra
                if isinstance(e, tuple) and e and e[0] == 'frame':
                    obj = mf.frame[e[1]]
                    mi.imm = obj.offset + (mi.imm or 0)
                    mi.extra = None
                    if mi.op in ('ld', 'st') and mi.addr is None and obj.id not in escaped: mi.addr = PRIVATE
                elif isinstance(e, tuple) and e and e[0] == 'tailarg':
                    mi.imm = F + e[1]
                    mi.extra = None
                    mi.addr = PRIVATE
                elif isinstance(e, tuple) and e and e[0] == 'arg' and mi.op == 'st' and mi.addr is None:
                    mi.addr = PRIVATE
        # Prologue.
        adjust = [MI('ialu', ['J27'], ['J27'], -F, '+')] if F else []
        pro = []
        if cj is not None: pro.append(MI('st', [], ['J27', 'CJMP'], cj, addr=PRIVATE, extra='save'))
        for prefix, q, ns, base in save_plan:
            for mi in _group_transfers(prefix, q, ns, base, store=True): pro.append(mi)
        entry = mf.blocks[0]
        live = physical_live_in(mf) if pro and 'kmirror' not in opt.DISABLED else None

        def mirrored(transfers, block, extra_avoid=()):
            if live is None: return transfers
            avoid = set(live[block.id]) | set(extra_avoid)
            for mi in block.mis: avoid |= reg_set(mi.d) | reg_set(mi.u)
            return _mirror(transfers, avoid)
        # Shrink-wrapping: save only on paths that need the saved state.
        wrap = shrink_wrap_point(mf, saves, cj is not None, F) if pro and 'shrinkwrap' not in opt.DISABLED else None
        region = None
        framed = None   # blocks that run with J27 adjusted (None: all)
        if wrap is not None:
            point, region, reloads = wrap
            pro += reloads
            outside = [b for b in mf.blocks if b.id not in region]
            if F and all(_frame_free(b, F) for b in outside):
                # The early exits touch only incoming arguments: they run on
                # the caller's J27, so allocate the frame at the save point too.
                for b in outside:
                    for mi in b.mis:
                        if mi.op in ('ld', 'st', 'ialu') and mi.u and mi.u[0] == 'J27' and mi.extra != 'save':
                            mi.imm = mi.imm - F
                point.mis[0:0] = adjust + mirrored(pro, point)
                framed = region
            else:
                entry.mis[0:0] = adjust
                point.mis[0:0] = mirrored(pro, point)
        else:
            entry.mis[0:0] = adjust + mirrored(pro, entry)
        # Epilogues.
        for b in mf.blocks:
            if not b.mis or b.mis[-1].op not in ('ret', 'tail'): continue
            ret = b.mis.pop()
            epi = []
            if region is None or b.id in region:
                for prefix, q, ns, base in save_plan:
                    epi += _group_transfers(prefix, q, ns, base, store=False)
                if cj is not None:
                    epi.append(MI('ld', ['CJMP'], ['J27'], cj, addr=PRIVATE, extra='save'))
            epi = mirrored(epi, b, reg_set(ret.u))
            if F and (framed is None or b.id in framed): epi.append(MI('ialu', ['J27'], ['J27'], F, '+'))
            ret.u = list(ret.u) + ['CJMP']
            b.mis.extend(epi); b.mis.append(ret)
        if 'spmirror' not in opt.DISABLED: sp_mirror(mf, physical_live_in(mf))

    # -- emission -------------------------------------------------------------
    def emit(self, mf):
        fn = self.fn
        Codegen.uid += 1
        tag = f'{Codegen.uid}'
        for b in mf.blocks:
            b.label = f'_cc_b{tag}_{b.id}'
        mf.blocks[0].label = fn.symbol
        if 'retdup' not in opt.DISABLED: duplicate_returns(mf)
        if 'cret' not in opt.DISABLED: conditional_returns(mf)
        thread_jumps(mf)
        order = layout(mf)
        self.divzero = f'_cc_dz{tag}'
        self.need_divzero = False
        # Upper bound on code words: <= 3 words per instruction, macros inline a loop.
        bound = sum(64 if mi.op == 'macro' else 3 for b in mf.blocks for mi in b.mis)
        _SHORT[0] = bound < SHORT_LIMIT and 'short' not in opt.DISABLED
        lines = [fn.symbol + ':']
        final = [finalize_branches(b, order[k+1] if k+1 < len(order) else None) for k, b in enumerate(order)]
        # Predecessor counts over the final control flow (explicit targets and fall-through).
        npred = {b.id: 0 for b in order}
        npred[mf.blocks[0].id] += 1   # the function entry
        for k, mis in enumerate(final):
            for mi in mis:
                for t in mi.t: npred[t.id] += 1
            if k+1 < len(order) and not (mis and mis[-1].op in TERM and mis[-1].op not in ('br', 'cret')):
                npred[order[k+1].id] += 1
        for k, b in enumerate(order):
            if k and b.label is not None and b.label != fn.symbol:
                lines.append(b.label + ':')
            nxt = final[k+1] if k+1 < len(order) and npred[order[k+1].id] == 1 else None
            lines += self.schedule(final[k], fallthrough=nxt)
        if self.need_divzero:
            lines += [self.divzero + ':', 'IDLE;;']
        for label, targets in mf.jtabs:
            name = f'{label}_{tag}'
            self.c.data += [name+':', '.word ' + ', '.join(t.label for t in targets)]
            for i, line in enumerate(lines):
                if label in line: lines[i] = re.sub(r'\b' + re.escape(label) + r'\b', name, line)
        return lines

    def expand_qcopy(self, mi):
        pk = _qcopy_packets(mi, f'_cc_qc{Codegen.uid}', _SHORT[0])
        return [p if p.endswith(':') else p + ';;' for p in pk]

    def schedule(self, mis, fallthrough=None):
        """Schedule one block. `fallthrough` is the machine instruction list of
        the next block when this block is its only predecessor: a closing
        conditional branch's packet may then take that block's leading
        instructions as ELSE operations (run only when the branch falls through)."""
        global _ALIAS
        _ALIAS = self.alias
        out = []
        region = []
        tail = []
        if len(mis) >= 2 and mis[-1].op == 'jmp' and mis[-2].op in ('br', 'cret'):
            # The conditional branch closes the block; the fall-back jump follows it.
            tail = [packet_text([classify(mis[-1])]) + ';;']
            mis = mis[:-1]
        fill = None
        if fallthrough is not None and not tail and mis and mis[-1].op in ('br', 'cret') and 'fill' not in opt.DISABLED:
            fill = lambda packets: fill_branch_packet(packets[-1], fallthrough)
        for mi in mis:
            if mi.op == 'macro':
                if region: out += schedule_region(region); region = []
                out += self.expand_macro(mi)
                continue
            region.append(mi)
            if mi.op in ('call', 'calli'):
                out += schedule_region(region); region = []
        if region: out += schedule_region(region, fill)
        return out + tail

    def expand_macro(self, mi):
        Codegen.uid += 1
        if mi.s == 'qcopy': return self.expand_qcopy(mi)
        L = f'_cc_dv{Codegen.uid}'
        self.need_divzero = True
        signed = mi.s == 'sdiv'
        q, r, t1, t2, jd, js = mi.d[:6]
        n, dv = mi.u
        N = lambda x: x[2:]
        pk = []
        if signed:
            an, ad, jn, jx = mi.d[6:10]
            pk.append(f'X R{N(an)} = ABS R{N(n)} (NF); {jn} = {n}')
            pk.append(f'X R{N(ad)} = ABS R{N(dv)} (NF); {jx} = {dv}')
            pk.append(f'{jx} = {jx} XOR {jn} (NF)')
            n, dv = an, ad
        pk.append(f'{r} = {n}; X R{N(t1)} = LD0 R{N(dv)}; X COMP(R{N(n)}, R{N(dv)}) (U); {q} = 0')
        pk.append(f'IF XALT, JUMP {L}_done (NP)(NF); {jd} = {dv}; X R{N(t2)} = LD0 R{N(n)}')
        pk.append(f'COMP({jd}, 0); X R{N(t1)} = R{N(t1)} - R{N(t2)} (NF)')
        pk.append(f'IF JEQ, JUMP {self.divzero} (NP)(NF); X R{N(t2)} = LSHIFT R{N(dv)} BY R{N(t1)}; {js} = {t1}')
        pk.append(f'COMP({js}, 0); LC0 = {t1}; X COMP(R{N(r)}, R{N(t2)}) (U)')
        pk.append(f'IF JEQ, JUMP {L}_last (NP)(NF)')
        pk.append(f'{L}_loop:')
        pk.append(f'IF NXALT (NF); DO X R{N(r)} = R{N(r)} - R{N(t2)}; DO {q} = {q} + 1; X R{N(t2)} = LSHIFT R{N(t2)} BY -1')
        pk.append(f'IF NLC0E, JUMP {L}_loop (NP)(NF); X COMP(R{N(r)}, R{N(t2)}) (U); {q} = {q} + {q}')
        pk.append(f'{L}_last:')
        pk.append(f'IF NXALT (NF); DO X R{N(r)} = R{N(r)} - R{N(t2)}; DO {q} = {q} + 1')
        pk.append(f'{L}_done:')
        if signed:
            pk.append(f'COMP({jx}, 0)')
            pk.append(f'IF JLT (NF); DO {q} = J31 - {q}')
            pk.append(f'COMP({jn}, 0)')
            pk.append(f'IF JLT (NF); DO X R{N(r)} = - R{N(r)}')
        return [p if p.endswith(':') else p + ';;' for p in pk]


def _needs_saves(b, saved, cjmp):
    """Does block b touch a saved callee-saved register or clobber CJMP?"""
    for mi in b.mis:
        if cjmp and (mi.op in ('call', 'calli', 'ijmp') or mi.extra == 'cjmp'): return True
        n = classify(mi)
        regs = n.reads | n.writes | n.treads | reg_set(mi.d) | reg_set(mi.u)
        if regs & saved: return True
    return False


def shrink_wrap_point(mf, saves, cjmp, F):
    """Pick the block where callee-saved registers (and CJMP) are saved.

    Returns (block, region, reloads): region (block ids) is everything
    reachable from the block, only enterable through it, not containing the
    entry, and holding every block that needs the saves; returns inside the
    region restore, the others (early exits) do not. A saved register that
    blocks outside the region use only for a constant or an incoming
    argument (loaded once, slot never written) moves to a free scratch
    register there and is rematerialized after the saves (`reloads`). None
    keeps the saves at the entry."""
    saved = reg_set(saves)
    entry = mf.blocks[0]
    succ = {b.id: succ_blocks(b) for b in mf.blocks}
    preds = {b.id: [] for b in mf.blocks}
    for b in mf.blocks:
        for t in succ[b.id]: preds[t.id].append(b)
    # Argument slots written anywhere (tail-call arguments, address-taken
    # arguments) cannot be reloaded.
    written = set(); addressed = False
    for b in mf.blocks:
        for mi in b.mis:
            if mi.op == 'st' and mi.u[0] == 'J27': written.add(mi.imm)
            if mi.op == 'ialu' and 'J27' in mi.u and mi.d != ['J27']: addressed = True
            if mi.op == 'macro': addressed = True
    candidates = []
    for point in mf.blocks[1:]:
        region = {point.id}; stack = [point]
        while stack:
            x = stack.pop()
            for t in succ[x.id]:
                if t.id not in region: region.add(t.id); stack.append(t)
        if entry.id in region: continue
        if any(p.id in region for p in preds[point.id]): continue   # inside a cycle
        if any(p.id not in region for x in region if x != point.id for p in preds[x]): continue
        candidates.append((point.freq, len(candidates), point, region))
    for _, _, point, region in sorted(candidates, key=lambda c: c[:2]):
        outside = [b for b in mf.blocks if b.id not in region]
        if any(_needs_saves(b, set(), cjmp) for b in outside): continue
        split = _split_arguments(outside, saved, F, written, addressed)
        if split is None: continue
        renames, reloads = split
        for b in outside:
            for mi in b.mis:
                mi.d = [renames.get(r, r) for r in mi.d]
                mi.u = [renames.get(r, r) for r in mi.u]
        return point, region, reloads
    return None


def _split_arguments(outside, saved, F, written, addressed):
    """Renames and reloads that free the blocks `outside` from saved registers."""
    used = set(); refs = {}
    for b in outside:
        for mi in b.mis:
            n = classify(mi)
            regs = n.reads | n.writes | n.treads | reg_set(mi.d) | reg_set(mi.u)
            used |= regs
            for r in regs & saved:
                refs.setdefault(r, []).append(mi)
    if not refs: return {}, []
    if addressed: return None
    renames = {}; reloads = []
    for r, mis in refs.items():
        defs = [mi for mi in mis if r in reg_set(mi.d) or r in classify(mi).writes]
        if len(defs) != 1: return None
        ld = defs[0]
        if ld.op == 'li' and ld.d == [r] and ld.extra is None:
            again = MI('li', [r], [], ld.imm, ld.s)   # constants rematerialize
        elif (ld.op == 'ld' and ld.d == [r] and len(ld.u) == 1 and ld.u[0] == 'J27' and ld.s not in ('L', 'Q')
                and ld.extra is None and (ld.imm or 0) >= F and ld.imm not in written):
            again = MI('ld', [r], ['J27'], ld.imm)
        else:
            return None
        # Only plain single-register operands: no pair, quad or implicit use.
        for mi in mis:
            n = classify(mi)
            if r not in mi.d and r not in mi.u: return None
            if any(':' in x for x in list(mi.d) + list(mi.u) if isinstance(x, str)): return None
            if mi.op in ('xmulw', 'xlsh') or mi.op == 'mov' and mi.s == 'Q': return None
        cls = regalloc.phys_class(r)
        free = [x for x in regalloc.CALLER_SAVED_LIST[cls] if x not in used and x not in renames.values()]
        if not free: return None
        renames[r] = free[0]; used.add(free[0])
        reloads.append(again)
    return renames, reloads


def _frame_free(b, F):
    """Block b addresses no frame word below the incoming arguments."""
    for mi in b.mis:
        if mi.op == 'macro' and 'J27' in mi.u: return False
        if mi.op in ('ld', 'st', 'ialu') and mi.u and mi.u[0] == 'J27':
            if mi.op == 'ialu' and mi.d == ['J27']: return False
            if len(mi.u) > (1 if mi.op in ('ld', 'ialu') else 2): return False   # indexed
            if isinstance(mi.extra, tuple) or (mi.imm or 0) < F: return False
        elif 'J27' in mi.u or 'J27' in mi.d: return False
    return True


def _quad_name(r):
    k = int(r[2:])
    return f'{r[:2]}{k+3}:{k}'


def _qcopy_packets(mi, L, short):
    """Copy 4*(2p+1+odd) words from any source alignment to a quad-aligned
    destination with data-alignment-buffer loads (PR 1-45, DAB loads): after a
    priming load, each `DAB Q[J += 4]` yields the next four source words.
    Reads stay inside the aligned quads that hold source words."""
    js, kd, ko, xa, xb = mi.d[:5]
    d, s, p, odd = mi.u
    A, B = _quad_name(xa), _quad_name(xb)
    S = '(S)' if short else ''
    # Operand transfers get their own packets: their IALU depends on the
    # classes the allocator picked for the inputs.
    return [
        f'{js} = {s}',
        f'{kd} = {d}',
        f'{ko} = {odd}',
        f'LC0 = {p}',
        f'{A} = DAB Q[{js} += 4]; COMP({ko}, 0)',
        f'{A} = DAB Q[{js} += 4]',
        f'{L}_loop:',
        f'Q[{kd} += 4] = {A}; {B} = DAB Q[{js} += 4]',
        f'IF NLC0E, JUMP {L}_loop (NP){S}; Q[{kd} += 4] = {B}; {A} = DAB Q[{js} += 4]',
        f'IF KEQ, JUMP {L}_done (NP){S}; Q[{kd} += 4] = {A}',
        f'{B} = DAB Q[{js} += 4]',
        f'Q[{kd} += 4] = {B}',
        f'{L}_done:',
    ]


MIRROR_MIN = 3   # save-area transfers that justify a K-IALU copy of J27


def _mirror(transfers, avoid):
    """Route every other save-area transfer through a scratch K register
    holding J27, so J-IALU and K-IALU transfers can share packets (each IALU
    issues one memory access per packet)."""
    saves = [mi for mi in transfers if mi.extra == 'save']
    if len(saves) < MIRROR_MIN: return transfers
    free = [k for k in regalloc.CALLER_SAVED_LIST['K'] if k not in avoid]
    if not free: return transfers
    k = free[0]
    out = [MI('mov', [k], ['J27'])]
    for n, mi in enumerate(transfers):
        if mi in saves and saves.index(mi) % 2 == 1:
            mi = MI(mi.op, mi.d, [k] + list(mi.u[1:]), mi.imm, mi.s, mi.t, mi.addr, mi.vol, mi.extra)
        out.append(mi)
    return out


def _sp_plain(mi):
    """A plain `[J27 + k]` access to private frame traffic (see isel.PRIVATE)."""
    return mi.op in ('ld', 'st') and mi.u and mi.u[0] == 'J27' and mi.addr is PRIVATE \
        and isinstance(mi.imm, int) and _plain(mi)


def sp_mirror(mf, live):
    """Balance frame traffic between the IALUs: every `[J27 + k]` access
    issues on the J-IALU, so a block segment dominated by argument stores
    and frame loads (a call's outgoing arguments, a function's incoming
    ones) serialises on J while K idles. Such a segment gets a scratch K
    copy of J27 and its least critical frame accesses go through it
    (marked 'spm' so the scheduler still compares their frame offsets)."""
    for b in mf.blocks:
        mis = b.mis
        avoid = set(live[b.id])
        for mi in mis: avoid |= reg_set(mi.d) | reg_set(mi.u)
        free = [k for k in regalloc.CALLER_SAVED_LIST['K'] if k not in avoid]
        out = []
        seg = []

        def flush(extra_j=0):
            if len(seg) >= 3:
                nodes = [classify(mi) for mi in seg]
                build_dag(nodes)
                # extra_j: the closing J27 release shares the last packets.
                j = sum(1 for n in nodes if n.unit == 'J') + extra_j; k = sum(1 for n in nodes if n.unit == 'K')
                # A K copy of J27 already made here (the save mirror) serves the
                # accesses after it, as long as nothing redefines it.
                reg = start = None
                for x, mi in enumerate(seg):
                    if mi.op == 'mov' and mi.u == ['J27'] and mi.d[0].startswith('K') and                             not any(mi.d[0] in reg_set(m.d) for m in seg[x+1:]):
                        reg, start = mi.d[0], x
                cands = [n for n in nodes if _sp_plain(n.mi) and (start is None or n.k > start)]
                m = min(len(cands), (j - k - (1 if reg is None else 0)) // 2)
                if m >= 1 and (reg is not None or free):
                    if reg is None:
                        reg = free.pop(0)
                        out.append(MI('mov', [reg], ['J27']))
                    for n in sorted(cands, key=lambda n: (n.height, -n.k))[:m]:
                        mi = n.mi
                        mi.u = [reg] + list(mi.u[1:])
                        if mi.extra != 'save': mi.extra = 'spm'
            out.extend(seg); seg.clear()
        for mi in mis:
            if mi.op in ('call', 'calli', 'macro') or 'J27' in reg_set(mi.d) or mi.op in TERM:
                flush(1 if 'J27' in reg_set(mi.d) and mi.op == 'ialu' else 0); out.append(mi); continue
            seg.append(mi)
        flush()
        b.mis = out


def _group_transfers(prefix, q, ns, base, store):
    """Save/restore registers of one aligned quad group with the widest transfers."""
    out = []
    lo = q*4
    present = set(ns)
    if len(present) >= 3 or present == {lo, lo+1, lo+2, lo+3}:
        # Restoring a whole quad would clobber caller-owned scratch in XR0..2 or J0..3
        # only if those are part of this group; group 0 is split below.
        if not (q == 0 and prefix in ('XR', 'J')) and not (prefix == 'J' and q == 2 and 8 in range(lo, lo+4) and 8 not in present):
            name = f'{prefix}{lo+3}:{lo}'
            if store: out.append(MI('st', [], ['J27', name], base, 'Q', addr=PRIVATE, extra='save'))
            else: out.append(MI('ld', [name], ['J27'], base, 'Q', addr=PRIVATE, extra='save'))
            return out
    k = lo
    while k < lo+4:
        if k in present and k+1 in present and k % 2 == 0:
            name = f'{prefix}{k+1}:{k}'
            if store: out.append(MI('st', [], ['J27', name], base + (k-lo), 'L', addr=PRIVATE, extra='save'))
            else: out.append(MI('ld', [name], ['J27'], base + (k-lo), 'L', addr=PRIVATE, extra='save'))
            k += 2; continue
        if k in present:
            name = f'{prefix}{k}'
            if store: out.append(MI('st', [], ['J27', name], base + (k-lo), addr=PRIVATE, extra='save'))
            else: out.append(MI('ld', [name], ['J27'], base + (k-lo), addr=PRIVATE, extra='save'))
        k += 1
    return out


# ---------------------------------------------------------------------------
# machine-level cleanups

def cleanup(mf):
    """Dead machine instruction removal and simple address folding (pre-RA)."""
    changed = True
    while changed:
        changed = False
        uses = {}
        for b in mf.blocks:
            for mi in b.mis:
                for x in mi.u:
                    if isinstance(x, int): uses[x] = uses.get(x, 0) + 1
        for b in mf.blocks:
            out = []
            for mi in b.mis:
                if mi.op in ('li', 'mov', 'ialu', 'xalu', 'xsh', 'xmul', 'xmulw', 'xlsh', 'xclz', 'macro', 'cset', 'cmov') or (mi.op == 'ld' and not mi.vol):
                    if mi.extra == 'cjmp' or mi.op == 'macro' and mi.s == 'qcopy': out.append(mi); continue
                    if mi.d and all(isinstance(x, int) and uses.get(x, 0) == (1 if mi.op in ('cset', 'cmov') and x in mi.u else 0) for x in mi.d):
                        changed = True
                        continue
                out.append(mi)
            b.mis = out
        # Compares whose flags nobody reads.
        for b in mf.blocks:
            out = []
            mis = b.mis
            for k, mi in enumerate(mis):
                if mi.op in ('icmp', 'xcmp'):
                    used = False
                    for later in mis[k+1:]:
                        if later.op in ('br', 'cset', 'cmov'): used = True; break
                        if later.op in ('icmp', 'xcmp', 'call', 'calli', 'macro'): break
                    if not used: changed = True; continue
                out.append(mi)
            b.mis = out
    fold_addresses(mf)
    if 'flags' not in opt.DISABLED: fuse_flags(mf)


def fuse_flags(mf):
    """Drop `COMP(v, 0)' when v was just produced by an ALU operation of the same
    unit: that operation's own EQ/LT flags are identical (signed compare with 0).
    The producer then writes flags and must stay out of sequencer packets."""
    zero_x = set()
    for b in mf.blocks:
        for mi in b.mis:
            if mi.op == 'li' and not mi.s and isinstance(mi.imm, int) and (mi.imm & 0xffffffff) == 0 and isinstance(mi.d[0], int):
                zero_x.add(mi.d[0])
    # A register that is written more than once (outside SSA) is only trusted within a block scan.
    for b in mf.blocks:
        mis = b.mis
        for k, mi in enumerate(mis):
            if mi.op == 'icmp' and len(mi.u) == 1 and isinstance(mi.imm, int) and s32(mi.imm) == 0 and mi.s != 'U':
                v = mi.u[0]; unit = mi.extra
            elif mi.op == 'xcmp' and len(mi.u) == 2 and mi.u[1] in zero_x and mi.s != 'U':
                v = mi.u[0]; unit = 'X'
            else:
                continue
            if not isinstance(v, int): continue
            # Find v's producer earlier in this block with no flag writer in between.
            prod = None
            for j in range(k-1, -1, -1):
                m = mis[j]
                if v in m.d:
                    if (unit == 'X' and m.op == 'xalu' and m.s not in ('PASS',)) or \
                       (unit in ('J', 'K') and m.op == 'ialu' and m.extra is None and m.s in ('+', '-', 'AND', 'OR', 'XOR', 'AND NOT')):
                        prod = m
                    break
                if m.op in ('icmp', 'xcmp', 'call', 'calli', 'macro', 'cset', 'cmov') or m.extra == 'flags':
                    break
            if prod is None: continue
            # Every consumer of these flags must see signed/equality semantics (it does: signed COMP).
            prod.extra = 'flags'
            mis[k] = None
        b.mis = [m for m in mis if m is not None]


def fold_addresses(mf):
    for b in mf.blocks:
        mis = b.mis
        uses = {}
        for bb in mf.blocks:
            for mi in bb.mis:
                for x in mi.u:
                    if isinstance(x, int): uses[x] = uses.get(x, 0) + 1
        defpos = {}
        for k, mi in enumerate(mis):
            if mi.op in ('ld', 'st') and isinstance(mi.u[0], int) and len(mi.u) == (1 if mi.op == 'ld' else 2) and mi.extra is None:
                base = mi.u[0]
                j = defpos.get(base)
                if j is not None:
                    # A multi-use address add stays, but the access reads its
                    # sources directly so it no longer waits for the add.
                    single = uses.get(base) == 1
                    dm = mis[j]
                    if dm is not None and dm.op == 'ialu' and dm.s == '+' and dm.extra is None and len(dm.d) == 1:
                        srcs = [x for x in dm.u if isinstance(x, int)]
                        clean = all(not any(x in m.d for m in mis[j+1:k] if m is not None) for x in dm.u)
                        if clean:
                            if len(dm.u) == 2 and (mi.imm or 0) == 0 and all(isinstance(x, int) for x in dm.u):
                                mi.u = [dm.u[0], dm.u[1]] + mi.u[1:]
                                mi.imm = None
                                if single: mis[j] = None
                            elif len(dm.u) == 1 and dm.imm is not None and isinstance(dm.imm, int) and isinstance(dm.u[0], int):
                                mi.u = [dm.u[0]] + mi.u[1:]
                                mi.imm = (mi.imm or 0) + s32(dm.imm)
                                if single: mis[j] = None
                    elif (dm is not None and dm.op == 'li' and dm.s and single and len(dm.d) == 1
                          and mf.cls.get(base) in ('J', 'K') and isinstance(mi.imm or 0, int) and 'absaddr' not in opt.DISABLED):
                        # A symbol address used once: one absolute access `[J31 + sym + k]`
                        # (J31/K31 read as zero) replaces the constant load and its latency.
                        mi.u = ['J31' if mf.cls[base] == 'J' else 'K31'] + mi.u[1:]
                        off = (dm.imm or 0) + (mi.imm or 0)
                        mi.imm = dm.s + (f' + {off}' if off > 0 else f' - {-off}' if off else '')
                        mis[j] = None
            for x in mi.d:
                defpos[x] = k
        b.mis = [m for m in mis if m is not None]


def _mi_regs(mi):
    """(defs, uses) as sets of physical register names, with wide operands expanded.

    Unlike the scheduling model, a return also *uses* its result registers."""
    n = classify(mi)
    if mi.op in ('ret', 'ijmp'):
        return n.writes, n.reads | reg_set(mi.u)
    return n.writes, n.reads


RENAMEABLE = {'li', 'mov', 'ialu', 'xalu', 'xsh', 'xmul', 'xclz', 'ld'}


def rename_registers(mf, min_freq=0, max_len=240):
    """Give block-local values the least recently used free register of their class.

    Post-allocation scheduling cannot overlap work that reuses a register (WAR/WAW);
    renaming a short-lived value to a register untouched during its lifetime
    removes those false dependences. Only scratch or already-saved registers are
    used, so no new saves are introduced.
    """
    _, live_out = regalloc.liveness(mf)
    usable = {c: [r for r in regs if r in regalloc.CALLER_SAVED or r in mf.used_regs]
              for c, regs in regalloc.REGS.items()}
    special = {'call', 'calli', 'macro', 'ret', 'ijmp', 'cset', 'cmov', 'br', 'jmp', 'idle'}
    for b in mf.blocks:
        mis = b.mis
        n = len(mis)
        if b.freq < min_freq or n < 4 or n > max_len: continue
        out = {r for r in live_out[b.id] if isinstance(r, str)}

        def analyse():
            info = [_mi_regs(mi) for mi in mis]
            live_after = [None]*n
            live = set(out)
            for k in range(n-1, -1, -1):
                live_after[k] = frozenset(live)
                d, u = info[k]
                live = (live - d) | {r for r in u if r in regalloc.ALLOCATABLE}
            return info, live_after, frozenset(live)
        info, live_after, live_in = analyse()
        last_ref = {}
        for k in range(n):
            mi = mis[k]
            d, u = info[k]
            r = mi.d[0] if len(mi.d) == 1 else None
            renamed = False
            if mi.op in RENAMEABLE and isinstance(r, str) and r in regalloc.ALLOCATABLE and mi.extra in (None, 'flags')                     and not (mi.op == 'ld' and (mi.s or len(mi.u) == 2 and False)) and d == {r} | ({'JSTAT', 'KSTAT', 'XSTAT'} & d):
                uses = []; stop = None; ok = True
                for j in range(k+1, n):
                    m = mis[j]; dj, uj = info[j]
                    if m.op in special and (r in uj or r in dj or m.op in ('call', 'calli', 'macro')):
                        ok = False; break
                    if r in uj:
                        if isinstance(m.extra, tuple) and m.extra[0] == 'post' and m.u and m.u[0] == r:
                            ok = False; break
                        if r not in m.u: ok = False; break   # implicit read (wide operand)
                        uses.append(j)
                    if r in dj:
                        stop = j; break
                    if r not in live_after[j]:
                        stop = j; break
                if ok and uses and (stop is not None or r not in out):
                    last = uses[-1]
                    busy = set(live_after[k-1] if k else live_in)
                    for j in range(k, last+1):
                        dj, uj = info[j]
                        busy |= dj | uj | live_after[j] if j < last else dj | uj
                    c = regalloc.phys_class(r)
                    cands = [x for x in usable.get(c, ()) if x not in busy and x != r]
                    if cands:
                        best = min(cands, key=lambda x: last_ref.get(x, -1))
                        if last_ref.get(best, -1) < last_ref.get(r, -1):
                            mi.d = [best]
                            for j in uses:
                                mis[j].u = [best if x == r else x for x in mis[j].u]
                            info, live_after, live_in = analyse()
                            d, u = info[k]
                            renamed = True
            for x in d | u: last_ref[x] = k


def post_ra(mf):
    """Post-increment addressing: [R + 0] then R = R + k  ->  [R += k]."""
    for b in mf.blocks:
        mis = b.mis
        for k, mi in enumerate(mis):
            if mi is None or mi.op not in ('ld', 'st') or mi.extra is not None or mi.vol and False: continue
            if len(mi.u) != (1 if mi.op == 'ld' else 2) or (mi.imm or 0) != 0 or mi.s not in (None, 'Q'): continue
            base = mi.u[0]
            if base in ('J27', 'J31', 'K31') or mi.op == 'ld' and base in mi.d: continue
            for j in range(k+1, len(mis)):
                m = mis[j]
                if m is None: continue
                if m.op == 'ialu' and m.d == [base] and m.u[0] == base and m.s == '+' and m.extra is None:
                    step = m.imm if len(m.u) == 1 else m.u[1]
                    if isinstance(step, str) and (step[0] != base[0]): break
                    if mi.s == 'Q' and (not isinstance(step, int) or s32(step) % 4): break
                    mi.extra = ('post', step if isinstance(step, str) else s32(step))
                    mis[j] = None
                    break
                if base in m.u or base in m.d or m.op in SEQ or m.op == 'macro': break
                if isinstance(m.u, list) and any(isinstance(x, str) and x == base for x in m.u): break
        b.mis = [m for m in mis if m is not None]


# ---------------------------------------------------------------------------
# pre-allocation scheduling

def _virtual(mi, cls, width):
    """Copy of mi with vregs renamed to class-typed placeholder registers."""
    def name(x):
        if not isinstance(x, int): return x
        c = cls.get(x, 'X')
        return ('XR' if c == 'X' else 'YR' if c == 'Y' else c) + str(1000 + 4*x)   # room for quad expansions
    v = MI(mi.op, [name(x) for x in mi.d], [name(x) for x in mi.u], mi.imm, mi.s, mi.t, mi.addr, mi.vol, mi.extra)
    return v


def prera_schedule(mf, min_freq=0):
    """Reorder hot blocks by a list schedule over virtual registers.

    Allocating registers after this order gives independent work (e.g. unrolled
    loop iterations) distinct registers, so the final post-allocation packing
    is not serialised by register reuse.
    """
    for b in mf.blocks:
        if b.freq < min_freq or len(b.mis) < (6 if b.freq >= 8 else 10): continue
        if any(mi.op in ('macro', 'call', 'calli') for mi in b.mis): continue
        mis = b.mis
        tail = []
        if len(mis) >= 2 and mis[-1].op == 'jmp' and mis[-2].op == 'br':
            tail = [mis[-1]]; mis = mis[:-1]
        vm = [_virtual(mi, mf.cls, mf.width) for mi in mis]
        nodes = [classify(v) for v in vm]
        build_dag(nodes)
        # Keep `y = f(...)` that feeds a later `x = y` copy (a phi) after the last
        # read of x, so x and y are never live together and coalesce.
        defs_at = {}
        for k, mi in enumerate(mis):
            for x in mi.d:
                if isinstance(x, int): defs_at[x] = k
        for k, mi in enumerate(mis):
            if mi.op == 'mov' and isinstance(mi.d[0], int) and isinstance(mi.u[0], int):
                x, y = mi.d[0], mi.u[0]
                j = defs_at.get(y)
                if j is None or j > k: continue
                for r, other in enumerate(mis[:j]):
                    # Only readers before the def: edges stay forward, so no cycles.
                    if x in other.u:
                        a, b_ = nodes[r], nodes[j]
                        if b_ is not a and not any(s_ is b_ for s_, _ in a.succ):
                            a.succ.append((b_, 0)); b_.preds.append((a, 0))
        for n in reversed(nodes):
            n.height = 1 + max((s_.height + lat for s_, lat in n.succ), default=0)
        packets = list_schedule(nodes)
        order = []
        for packet in packets:
            packet.sort(key=lambda n: n.k)
            order += [mis[n.k] for n in packet]
        b.mis = order + tail


# ---------------------------------------------------------------------------
# layout

def thread_jumps(mf):
    changed = True
    while changed:
        changed = False
        for b in mf.blocks[1:]:
            if len(b.mis) == 1 and b.mis[0].op == 'jmp':
                target = b.mis[0].t[0]
                if target is b: continue
                for p in mf.blocks:
                    for mi in p.mis:
                        if mi.op in ('jmp', 'br', 'ijmp') and b in mi.t:
                            mi.t = [target if x is b else x for x in mi.t]
                            changed = True
                    p.succs = [target if x is b else x for x in p.succs]
                # Jump tables name their targets separately from the ijmp.
                mf.jtabs = [(label, [target if x is b else x for x in ts]) for label, ts in mf.jtabs]
                if changed:
                    mf.blocks.remove(b)
                    break
    # Drop unreachable blocks.
    reach = {mf.blocks[0].id}; stack = [mf.blocks[0]]
    while stack:
        b = stack.pop()
        for mi in b.mis:
            for t in mi.t:
                if t.id not in reach: reach.add(t.id); stack.append(t)
    mf.blocks = [b for b in mf.blocks if b.id in reach]


RETDUP_LIMIT = 8   # machine instructions of a return block copied over a jump


def duplicate_returns(mf):
    """Replace `JUMP T` by a copy of T when T is a short block that returns
    (epilogue + CJMP, a tail-call jump, or IDLE): the copy schedules with the
    jumping block's own tail, saving the jump and at least one packet."""
    entry = mf.blocks[0]
    for b in mf.blocks:
        if not b.mis or b.mis[-1].op != 'jmp': continue
        t = b.mis[-1].t[0]
        if t is b or t is entry or not t.mis or t.mis[-1].op not in ('ret', 'tail', 'idle'): continue
        if len(t.mis) > RETDUP_LIMIT or any(mi.op in ('macro', 'call', 'calli') for mi in t.mis): continue
        b.mis[-1:] = [MI(mi.op, mi.d, mi.u, mi.imm, mi.s, mi.t, mi.addr, mi.vol, mi.extra) for mi in t.mis]
        b.succs = []


CRET_OPS = 1   # register-only instruction of a return block hoisted above a branch
_CRET_OK = {'li', 'mov'}   # IALU slots: they rarely collide with the compare


def physical_live_in(mf):
    """Live-in physical registers per block id (post-allocation, conservative)."""
    info = {}
    for b in mf.blocks:
        gen = set(); kill = set()
        for mi in b.mis:
            n = classify(mi)
            reads = n.reads | n.treads | reg_set(mi.u)
            if mi.op in ('ret', 'tail'): reads |= {'XR0', 'J27', 'CJMP'} | set(CALLEE_SAVED)
            if mi.op in ('call', 'calli'): reads |= {'J27'} | set(CALLEE_SAVED)
            gen |= reads - kill
            if mi.op not in ('cset', 'cmov'): kill |= n.writes | reg_set(mi.d)
        info[b.id] = (gen, kill, succ_blocks(b))
    live = {b.id: set() for b in mf.blocks}
    changed = True
    while changed:
        changed = False
        for b in reversed(mf.blocks):
            gen, kill, succs = info[b.id]
            out = set()
            for t in succs: out |= live[t.id]
            new = gen | (out - kill)
            if new != live[b.id]: live[b.id] = new; changed = True
    return live


def conditional_returns(mf):
    """A conditional branch to a return block with nothing left to restore
    becomes a conditional return `IF cond, CJMP (ABS)` followed by a jump to
    the other successor (usually dropped by layout). Up to CRET_OPS register
    instructions of the return block (typically setting XR0) move above the
    branch when the other successor does not need the registers they write."""
    live = None
    succ = {b.id: succ_blocks(b) for b in mf.blocks}

    def reaches(a, z):
        seen = {a.id}; stack = [a]
        while stack:
            x = stack.pop()
            if x is z: return True
            for t in succ[x.id]:
                if t.id not in seen: seen.add(t.id); stack.append(t)
        return False
    for b in mf.blocks:
        if not b.mis or b.mis[-1].op != 'br': continue
        br = b.mis[-1]
        yes, no = br.t
        for t, other, cond in ((yes, no, br.s), (no, yes, br.s[1:] if br.s.startswith('N') else 'N' + br.s)):
            if t is b or not t.mis or t.mis[-1].op != 'ret' or len(t.mis) > CRET_OPS + 1: continue
            # A loop's back edge must stay the branch: the jump would run every iteration.
            if reaches(other, b): continue
            ops = t.mis[:-1]
            if ops:
                # Only operations that cannot need an immediate extension, so they
                # can share the compare's packet rather than add one.
                if any(mi.op not in _CRET_OK or mi.extra is not None or classify(mi).ext for mi in ops): continue
                if live is None: live = physical_live_in(mf)
                wr = set()
                for mi in ops: wr |= classify(mi).writes | reg_set(mi.d)
                if wr & (live[other.id] | _FILL_SPECIAL): continue
            ret = t.mis[-1]
            b.mis[-1:] = [MI(mi.op, mi.d, mi.u, mi.imm, mi.s, mi.t, mi.addr, mi.vol, mi.extra) for mi in ops] + \
                         [MI('cret', [], list(ret.u), None, cond, extra=br.extra), MI('jmp', t=[other])]
            live = None   # the block now reads what the hoisted instructions read
            break


def succ_blocks(b):
    t = [x for mi in b.mis for x in mi.t]
    out = []
    for x in t:
        if x not in out: out.append(x)
    return out


def layout(mf):
    placed = set(); order = []
    blocks = mf.blocks
    pos = {b.id: k for k, b in enumerate(blocks)}
    preds = {b.id: 0 for b in blocks}
    for b in blocks:
        for s in succ_blocks(b): preds[s.id] += 1
    queue = list(blocks)
    while len(order) < len(blocks):
        b = next(x for x in queue if x.id not in placed)
        while b is not None and b.id not in placed:
            placed.add(b.id); order.append(b)
            term = b.mis[-1] if b.mis else None
            nxt = None
            if term is not None and term.op == 'jmp':
                cand = term.t[0]
                if cand.id not in placed: nxt = cand
            elif term is not None and term.op == 'br':
                cands = [x for x in term.t if x.id not in placed]
                if cands:
                    # Prefer the colder successor as fall-through only when the hot one
                    # is a loop back edge; otherwise fall into the more frequent one,
                    # and on a tie into the one that does not return at once (early
                    # returns are the less likely direction).
                    cands.sort(key=lambda x: (-x.freq, _returns(x), pos[x.id]))
                    nxt = cands[0]
                    if len(cands) == 2 and cands[0].depth < b.depth <= cands[1].depth:
                        nxt = cands[1]
            b = nxt
    return order


def _returns(b):
    """Block b returns (or halts) directly or through one jump."""
    end = b.mis[-1] if b.mis else None
    if end is not None and end.op == 'jmp':
        t = end.t[0]
        end = t.mis[-1] if t.mis else None
    return end is not None and end.op in ('ret', 'tail', 'idle')


def finalize_branches(b, nxt):
    mis = list(b.mis)
    if not mis: return mis
    t = mis[-1]
    if t.op == 'jmp':
        if nxt is not None and t.t[0] is nxt: mis.pop()
        return mis
    if t.op == 'br':
        yes, no = t.t
        cond = t.s
        if nxt is not None and yes is nxt:
            cond = cond[1:] if cond.startswith('N') else 'N' + cond
            yes, no = no, yes
        br = MI('br', [], [], None, cond, t=[yes])
        mis[-1] = br
        if not (nxt is not None and no is nxt):
            mis.append(MI('jmp', t=[no]))
        return mis
    return mis


# ---------------------------------------------------------------------------
# rendering

def rn(r):
    return r[2:] if r.startswith(('XR', 'YR')) else r[1:]


def imm_text(v):
    if isinstance(v, str): return v
    return str(s32(v))


def render(mi, seq_packet=False):
    op = mi.op
    nf = '' if seq_packet or mi.extra == 'flags' else ' (NF)'
    d = mi.d; u = mi.u
    if op == 'li':
        if mi.s:
            return f'{d[0]} = {mi.s}' + (f' + {mi.imm}' if mi.imm and mi.imm > 0 else f' - {-mi.imm}' if mi.imm else '')
        return f'{d[0]} = {imm_text(mi.imm)}'
    if op == 'mov':
        if mi.s == 'Q':
            a, b = int(d[0][2:]), int(u[0][2:])
            return f'XR{a+3}:{a} = XR{b+3}:{b}'
        return f'{d[0]} = {u[0]}'
    if op == 'ialu':
        if mi.extra == 'cjmp':
            return f'{d[0]} = {u[0]} + 0 (CJMP)'
        if mi.s in ('MIN', 'MAX'):
            return f'{d[0]} = {mi.s}({u[0]}, {u[1]}){nf}'
        if len(u) == 2:
            return f'{d[0]} = {u[0]} {mi.s} {u[1]}{nf}'
        if isinstance(mi.imm, str):
            return f'{d[0]} = {u[0]} {mi.s} {mi.imm}{nf}'
        v = s32(mi.imm)
        # Flip the sign only when the magnitude still fits imm8 (-128 stays literal).
        if mi.s == '+' and -127 <= v < 0: return f'{d[0]} = {u[0]} - {-v}{nf}'
        if mi.s == '-' and -127 <= v < 0: return f'{d[0]} = {u[0]} + {-v}{nf}'
        return f'{d[0]} = {u[0]} {mi.s} {v}{nf}'
    if op == 'icmp':
        rhs = u[1] if len(u) == 2 else mi.imm if isinstance(mi.imm, str) else str(s32(mi.imm))
        return f'COMP({u[0]}, {rhs})' + (' (U)' if mi.s == 'U' else '')
    B = 'Y' if d and isinstance(d[0], str) and d[0].startswith('YR') else 'X'
    if op == 'xalu':
        if mi.s == 'NEG': return f'{B} R{rn(d[0])} = - R{rn(u[0])}{nf}'
        if mi.s in ('NOT', 'ABS', 'PASS', 'INC', 'DEC'): return f'{B} R{rn(d[0])} = {mi.s} R{rn(u[0])}{nf}'
        if mi.s in ('MIN', 'MAX'): return f'{B} R{rn(d[0])} = {mi.s}(R{rn(u[0])}, R{rn(u[1])}){nf}'
        if mi.s == 'CLIP': return f'{B} R{rn(d[0])} = CLIP R{rn(u[0])} BY R{rn(u[1])}{nf}'
        return f'{B} R{rn(d[0])} = R{rn(u[0])} {mi.s} R{rn(u[1])}{nf}'
    if op == 'xcmp':
        return f'X COMP(R{rn(u[0])}, R{rn(u[1])})' + (' (U)' if mi.s == 'U' else '')
    if op == 'xsh':
        by = str(mi.imm) if len(u) == 1 else f'R{rn(u[1])}'
        return f'{B} R{rn(d[0])} = {mi.s} R{rn(u[0])} BY {by}{nf}'
    if op == 'xmul':
        # (T): fractional product (a*b) >> 31, truncated; otherwise integer.
        return f'{B} R{rn(d[0])} = R{rn(u[0])} * R{rn(u[1])} ({mi.s or "I"}){nf}'
    if op == 'xmulw':
        k = int(rn(d[0]))
        return f'X R{k+1}:{k} = R{rn(u[0])} * R{rn(u[1])} (I)' + ('(U)' if mi.s == 'U' else '') + nf
    if op == 'xlsh':
        k = int(rn(d[0])); j = int(rn(u[0]))
        return f'X LR{k+1}:{k} = {mi.s} R{j+1}:{j} BY {mi.imm}{nf}'
    if op == 'xclz':
        return f'{B} R{rn(d[0])} = LD0 R{rn(u[0])}{nf}'
    if op in ('ld', 'st'):
        size = f'{mi.s} ' if mi.s in ('L', 'Q') else ''
        base = u[0]
        post = mi.extra[1] if isinstance(mi.extra, tuple) and mi.extra[0] == 'post' else None
        if post is not None:
            addr = f'[{base} += {post}]'
        elif op == 'ld' and len(u) == 2 or op == 'st' and len(u) == 3:
            addr = f'[{base} + {u[1]}]'
        elif isinstance(mi.imm, str):
            addr = f'[{base} + {mi.imm}]'
        else:
            v = s32(mi.imm or 0)
            addr = f'[{base} + {v}]'
        data = d[0] if op == 'ld' else u[-1]
        if mi.s in ('L', 'Q') and ':' not in data and data.startswith(('XR', 'YR')):
            k0 = int(data[2:]); data = f'{data[:2]}{k0 + (3 if mi.s == "Q" else 1)}:{k0}'
        if op == 'ld': return f'{data} = {size}{addr}'
        return f'{size}{addr} = {data}'
    raise AssertionError(f'render {op}')


def render_seq(mi, labels=True):
    op = mi.op
    short = '(S)' if _SHORT[0] else ''
    if op == 'br': return f'IF {mi.s}, JUMP {mi.t[0].label} (NP)(NF){short}'
    if op == 'jmp': return f'JUMP {mi.t[0].label} (NP)(NF){short}'
    if op == 'call': return f'CALL {mi.s} (NP)(NF)'
    if op == 'calli': return 'CJMP_CALL (NP)(ABS)(NF)'
    if op in ('ret', 'ijmp'): return 'CJMP (NP)(ABS)(NF)'
    if op == 'tail': return f'JUMP {mi.s} (NP)(NF)'
    if op == 'cret': return f'IF {mi.s}, CJMP (NP)(ABS)(NF)'
    if op == 'idle': return 'IDLE'
    if op == 'cset': return f'IF {mi.s} (NF); DO {mi.d[0]} = {s32(mi.imm if mi.imm is not None else 1)}'
    if op == 'cmov': return f'IF {mi.s} (NF); DO {mi.d[0]} = {mi.u[1]}'
    raise AssertionError(op)


# ---------------------------------------------------------------------------
# scheduling

def reg_set(names):
    out = set()
    for r in names:
        if not isinstance(r, str): continue
        m = re.fullmatch(r'(XR|YR|J|K)(\d+):(\d+)', r)
        if m:
            p = m[1]
            for k in range(int(m[3]), int(m[2])+1): out.add(f'{p}{k}')
        else:
            out.add(r)
    return out


class Node:
    __slots__ = ('mi', 'k', 'reads', 'writes', 'treads', 'succ', 'npred', 'preds', 'cycle', 'height',
                 'words', 'ext', 'unit', 'block', 'cu', 'xin', 'xout', 'yin', 'yout', 'jport', 'kport', 'sport', 'cross', 'const', 'seq', 'mem')


XPORT = re.compile(r'XR\d')


def classify(mi):
    """Fill resource information of a machine instruction."""
    n = Node(); n.mi = mi
    op = mi.op
    d = reg_set(mi.d); u = reg_set(mi.u)
    if op == 'mov' and mi.s == 'Q':
        for regs_, tgt in ((mi.d, d), (mi.u, u)):
            if regs_ and isinstance(regs_[0], str) and regs_[0].startswith('XR'):
                k0 = int(regs_[0][2:]); tgt.update(f'XR{k0+j}' for j in range(4))
    # Wide transfers named by their first register cover 2 or 4 registers.
    if op in ('ld', 'st') and mi.s in ('L', 'Q'):
        w = 4 if mi.s == 'Q' else 2
        data = mi.d[0] if op == 'ld' else mi.u[-1]
        if ':' not in data and data.startswith(('XR', 'YR')):
            k0 = int(data[2:])
            (d if op == 'ld' else u).update(f'{data[:2]}{k0+j}' for j in range(w))
    # Register-pair operands occupy two registers.
    for which, regs, target in (('d', mi.d, d), ('u', mi.u, u)):
        for k, r in enumerate(regs):
            if (op, which, k) in regalloc.PAIR_POSITIONS:
                target.add(f'XR{int(r[2:])+1}')
    n.reads = set(u); n.writes = set(d); n.treads = set()
    n.words = 1; n.ext = False; n.unit = None; n.block = None; n.cu = None
    n.xin = n.xout = n.yin = n.yout = n.jport = n.kport = n.sport = 0; n.cross = False; n.const = False
    n.seq = op in SEQ; n.mem = None
    if op in ('ialu', 'icmp'):
        regs = [r for r in list(mi.u)+list(mi.d) if isinstance(r, str) and r not in ('CJMP',)]
        n.unit = 'K' if any(r.startswith('K') for r in regs) else 'J'
        if op == 'icmp' or mi.extra == 'flags': n.writes.add('KSTAT' if n.unit == 'K' else 'JSTAT')
        imm = mi.imm if (op == 'ialu' and len(mi.u) == 1 and mi.extra != 'cjmp') or (op == 'icmp' and len(mi.u) == 1) else None
        if isinstance(imm, str) or (imm is not None and not -128 <= s32(imm) <= 127):
            n.words = 2; n.ext = True
        if mi.extra == 'cjmp': n.writes.add('CJMP')
    elif op in ('xalu', 'xcmp', 'xsh', 'xmul', 'xmulw', 'xlsh', 'xclz'):
        first = (mi.d or mi.u)[0]
        n.block = 'Y' if isinstance(first, str) and first.startswith('YR') else 'X'
        n.cu = 'alu' if op in ('xalu', 'xcmp') else 'mul' if op in ('xmul', 'xmulw') else 'shift'
        if op == 'xcmp' or mi.extra == 'flags': n.writes.add('YSTAT' if n.block == 'Y' else 'XSTAT')
    elif op == 'li':
        dst = mi.d[0]
        if mi.s or not -16384 <= s32(mi.imm) <= 16383: n.words = 2; n.ext = True
        n.const = True
        if dst.startswith('J'): n.unit = 'J'; n.jport = 1
        elif dst.startswith('K'): n.unit = 'K'; n.kport = 1
        elif dst.startswith('YR'): n.unit = 'flex'; n.yin = 1
        else: n.unit = 'flex'; n.xin = 1
    elif op == 'mov':
        dst, src = mi.d[0], mi.u[0]
        n.treads = set(u)
        regs = [dst, src]
        hasj = any(r.startswith('J') for r in regs); hask = any(r.startswith('K') for r in regs)
        n.unit = 'K' if hask else 'J' if hasj else 'flex'
        if hasj: n.jport = 1
        if hask: n.kport = 1
        if hasj and hask: n.cross = True
        if dst.startswith('XR'): n.xin = 1
        if src.startswith('XR'): n.xout = 1
        if dst.startswith('YR'): n.yin = 1
        if src.startswith('YR'): n.yout = 1
        if any(r in ('CJMP', 'LC0', 'LC1') for r in regs): n.sport = 1
    elif op in ('ld', 'st'):
        base = mi.u[0]
        n.unit = 'K' if base.startswith('K') else 'J'
        data = mi.d[0] if op == 'ld' else mi.u[-1]
        post = isinstance(mi.extra, tuple) and mi.extra[0] == 'post'
        if post: n.writes.add(base)
        indexed = (op == 'ld' and len(mi.u) == 2) or (op == 'st' and len(mi.u) == 3)
        if not post and not indexed:
            if isinstance(mi.imm, str) or not -128 <= s32(mi.imm or 0) <= 127: n.words = 2; n.ext = True
        if post and isinstance(mi.extra[1], int) and not -128 <= mi.extra[1] <= 127:
            n.words = 2; n.ext = True
        if data.startswith('XR'):
            if op == 'ld': n.xin += 1
            else: n.xout += 1
        if data.startswith('YR'):
            if op == 'ld': n.yin += 1
            else: n.yout += 1
        if data.startswith('J'):
            n.jport = 1
            if n.unit == 'K': n.cross = True
        if data.startswith('K'):
            n.kport = 1
            if n.unit == 'J': n.cross = True
        if data == 'CJMP': n.sport = 1
        if op == 'st':
            n.treads = reg_set([data])
            if mi.s in ('L', 'Q') and ':' not in data and data.startswith(('XR', 'YR')):
                k0 = int(data[2:]); n.treads = {f'{data[:2]}{k0+j}' for j in range(4 if mi.s == 'Q' else 2)}
        n.mem = ('ld' if op == 'ld' else 'st', mi.addr, mi.vol or mi.extra == 'save')
    elif op in ('cset', 'cmov'):
        c = mi.extra
        n.reads.add('XSTAT' if c == 'X' else 'KSTAT' if c == 'K' else 'JSTAT')
        dst = mi.d[0]
        if op == 'cset':
            n.words = 2 if -16384 <= s32(mi.imm if mi.imm is not None else 1) <= 16383 else 3
            if dst.startswith('J'): n.jport = 1; n.unit = 'J'
            elif dst.startswith('K'): n.kport = 1; n.unit = 'K'
            elif dst.startswith('YR'): n.yin = 1; n.unit = 'flex'
            else: n.xin = 1; n.unit = 'flex'
            n.const = True
        else:
            n.words = 2
            src = mi.u[1]
            regs = [dst, src]
            hasj = any(r.startswith('J') for r in regs); hask = any(r.startswith('K') for r in regs)
            n.unit = 'K' if hask else 'J' if hasj else 'flex'
            if hasj: n.jport = 1
            if hask: n.kport = 1
            if hasj and hask: n.cross = True
            if dst.startswith('XR'): n.xin = 1
            if src.startswith('XR'): n.xout = 1
            if dst.startswith('YR'): n.yin = 1
            if src.startswith('YR'): n.yout = 1
            n.treads = {src}
    elif op in ('br', 'cret'):
        n.words = 1 if _SHORT[0] or op == 'cret' else 2
        if op == 'cret': n.reads.add('CJMP')
        cond = mi.s[1:] if mi.s.startswith('N') else mi.s
        if cond.startswith('LC'):
            # Evaluating LCnE decrements the counter.
            n.reads.add(cond[:3]); n.writes.add(cond[:3])
        else:
            n.reads.add('XSTAT' if cond.startswith('X') else 'KSTAT' if cond.startswith('K') else 'JSTAT')
        n.ext = not _SHORT[0] and op == 'br'
    elif op in ('jmp', 'call'):
        n.words = 2; n.ext = True
        if op == 'jmp' and _SHORT[0]: n.words = 1; n.ext = False
        if op == 'call': n.writes |= set(regalloc.CALLER_SAVED) | {'CJMP', 'JSTAT', 'KSTAT', 'XSTAT', 'LC0', 'LC1'}; n.mem = ('call', None, True)
    elif op == 'calli':
        n.reads.add('CJMP')
        n.writes |= set(regalloc.CALLER_SAVED) | {'CJMP', 'JSTAT', 'KSTAT', 'XSTAT', 'LC0', 'LC1'}; n.mem = ('call', None, True)
    elif op in ('ret', 'ijmp'):
        # Only CJMP is read at issue; results written in the same packet are
        # committed before the caller resumes.
        n.reads = {'CJMP'}
    elif op == 'tail':
        # The callee returns through CJMP; stores in this packet commit before it runs.
        n.words = 2; n.ext = True
        n.reads = {'CJMP'}; n.mem = ('call', None, True)
    return n


def may_alias(a, b):
    """Memory dependence test between two scheduled memory nodes."""
    ka, ad, va = a.mem; kb, bd, vb = b.mem
    if ka == 'call' or kb == 'call': return True
    if ad is PRIVATE or bd is PRIVATE:
        ma, mb = a.mi, b.mi
        base_a = 'J27' if ma.extra in ('save', 'spm') else ma.u[0]
        base_b = 'J27' if mb.extra in ('save', 'spm') else mb.u[0]
        if base_a == 'J27' and base_b == 'J27' and _plain(ma) and _plain(mb):
            wa = 4 if ma.s == 'Q' else 2 if ma.s == 'L' else 1
            wb = 4 if mb.s == 'Q' else 2 if mb.s == 'L' else 1
            x, y = s32(ma.imm or 0), s32(mb.imm or 0)
            return not (x + wa <= y or y + wb <= x)
        # The other access goes through a pointer (or an indexed frame address).
        mine, other = (ma, mb) if ad is PRIVATE else (mb, ma)
        return (other.extra in ('save', 'spm') or other.u[0] == 'J27') and not _plain(other)
    if ad is None or bd is None:
        # Save-area and argument traffic: compare concrete SP offsets.
        ma, mb = a.mi, b.mi
        # Save-area transfers may address the frame through a K copy of J27.
        base_a = 'J27' if ma.extra in ('save', 'spm') else ma.u[0]
        base_b = 'J27' if mb.extra in ('save', 'spm') else mb.u[0]
        if base_a == 'J27' and base_b == 'J27' and _plain(ma) and _plain(mb):
            wa = 4 if ma.s == 'Q' else 2 if ma.s == 'L' else 1
            wb = 4 if mb.s == 'Q' else 2 if mb.s == 'L' else 1
            x, y = s32(ma.imm or 0), s32(mb.imm or 0)
            return not (x + wa <= y or y + wb <= x)
        if (ad is None) != (bd is None):
            # An SP-relative access cannot alias memory reached through a non-frame address.
            other = bd if ad is None else ad
            mine = a.mi if ad is None else b.mi
            if (mine.u[0] == 'J27' or mine.extra == 'spm') and _plain(mine) and other.kind in ('sym', 'symvar'): return False
        return True
    return _ALIAS.may_alias(ad, bd)


def _plain(mi):
    return not (isinstance(mi.extra, tuple) and mi.extra[0] == 'post') and not (mi.op == 'ld' and len(mi.u) == 2 or mi.op == 'st' and len(mi.u) == 3)


class _AnyAlias:
    def may_alias(self, a, b):
        from .opt import AliasInfo
        return AliasInfo.may_alias(self, a, b)

    def reachable_by_pointer(self, a):
        return True


_ALIAS = _AnyAlias()


def _frame_access(mi):
    """A memory access that may touch this function's frame."""
    a = mi.addr
    return (mi.u and mi.u[0] == 'J27') or mi.extra in ('save', 'spm') or a is PRIVATE         or getattr(a, 'kind', None) in ('frame', 'framevar')


def build_dag(nodes):
    last_w = {}
    readers = {}
    mems = []
    sp_writes = []
    for k, n in enumerate(nodes):
        n.k = k; n.succ = []; n.preds = []; n.cycle = None
    for n in nodes:
        def edge(p, lat):
            p.succ.append((n, lat)); n.preds.append((p, lat))
        for r in n.reads:
            w = last_w.get(r)
            if w is not None: edge(w, 1)
        for r in n.writes:
            w = last_w.get(r)
            if w is not None: edge(w, 1)
            for rd in readers.get(r, ()):
                if rd is n: continue
                # A call clobbers scratch registers only after its packet: a
                # store in the call's packet still reads the old value.
                edge(rd, 1 if r in rd.treads and not (n.mi.op in ('call', 'calli') and r in regalloc.CALLER_SAVED) else 0)
        if 'J27' in n.writes and n.mi.op not in ('call', 'calli'):
            # Frame allocation or release. No red zone exists below J27 (an
            # interrupt pushes there), so every memory access before it in
            # program order issues no later than its packet; frame accesses
            # after it (also through a K/J mirror of J27) wait for it.
            for m in mems: edge(m, 0)
            sp_writes.append(n)
        if n.mem is not None and n.mem[0] != 'call' and _frame_access(n.mi):
            for w in sp_writes: edge(w, 1)
        if n.mem is not None:
            kind, _, vol = n.mem
            for m in mems:
                mk = m.mem[0]
                if kind == 'ld' and mk == 'ld' and not (vol and m.mem[2]): continue
                if kind == 'call':
                    edge(m, 0); continue
                if (vol or m.mem[2]) and mk != 'call':
                    if n.mi.extra == 'save' or m.mi.extra == 'save':
                        if not may_alias(m, n): continue
                    edge(m, 1); continue
                if not may_alias(m, n): continue
                # Stores issued in a call's packet commit before the callee runs.
                edge(m, 0 if kind == 'call' else 1)
            mems.append(n)
        for r in n.writes:
            last_w[r] = n; readers[r] = []
        for r in n.reads:
            readers.setdefault(r, []).append(n)
    # Terminators and calls close the region.
    last = nodes[-1]
    if last.mi.op in TERM or last.mi.op in ('call', 'calli'):
        for n in nodes[:-1]:
            if not any(s is last for s, _ in n.succ):
                n.succ.append((last, 0)); last.preds.append((n, 0))
    # Heights.
    for n in reversed(nodes):
        n.height = 0
        for s, lat in n.succ:
            n.height = max(n.height, s.height + lat + (1 if lat == 0 else 0)*0)
        n.height += 1


def fits(packet, n):
    items = packet + [n]
    words = sum(x.words for x in items)
    if words > 4: return False
    seqs = [x for x in items if x.seq]
    if len(seqs) > 1: return False
    # Sequencer packets carry (NF), which would suppress a fused flag producer.
    if seqs and any(x.mi.extra == 'flags' for x in items): return False
    exts = [x for x in items if x.ext]
    if seqs:
        s = seqs[0]
        if s.ext and len(exts) > 1: return False
        if not s.ext and len(exts) > 1: return False
        if s.mi.op == 'cset' and exts: return False
    else:
        if len(exts) > 1: return False
    # IALU units.
    j = sum(1 for x in items if x.unit == 'J'); k = sum(1 for x in items if x.unit == 'K')
    flex = sum(1 for x in items if x.unit == 'flex')
    if j > 1 or k > 1 or j + k + flex > 2: return False
    # Compute block units.
    for blk in ('X', 'Y'):
        cus = [x.cu for x in items if x.block == blk]
        if len(cus) > 2 or len(set(cus)) != len(cus): return False
    # Ports.
    if sum(x.xout for x in items) > 1 or sum(x.xin for x in items) > 2: return False
    if sum(x.yout for x in items) > 1 or sum(x.yin for x in items) > 2: return False
    if sum(x.jport for x in items) > 1 or sum(x.kport for x in items) > 1 or sum(x.sport for x in items) > 1: return False
    if any(x.cross for x in items) and any(x.const for x in items if x.mi.op in ('li', 'cset')): return False
    # TS201 Programming Reference, Table 1-1 note 5 (p. 1-35): two
    # external loads cannot target the same register quad in one packet,
    # even when the individual destination registers differ. Memory pointers
    # here are not proven internal, so conservatively separate these loads.
    # Immediate constants and core register moves do not use external reads.
    load_quads = set()
    for x in items:
        if x.mi.op != 'ld': continue
        quads = set()
        for r in reg_set(x.mi.d):
            m = re.fullmatch(r'(XR|YR|J|K)(\d+)', r)
            if m: quads.add((m[1], int(m[2]) // 4))
        if quads & load_quads: return False
        load_quads |= quads
    # No register written twice; transfers must not read a register written in the packet.
    written = set()
    for x in items:
        if x.writes & written: return False
        written |= x.writes
    for x in items:
        if x.mi.op in ('call', 'calli'): written -= x.writes & regalloc.CALLER_SAVED
    for x in items:
        if x.treads & (written - x.writes): return False
        if x.treads & x.writes and x.mi.op != 'ld': pass
    return True


@lru_cache(maxsize=200000)
def packet_ok(text):
    from ts201.assembler import packet, AssemblyError
    try:
        packet(text, layout=True); return True
    except (AssemblyError, ValueError):
        return False


def packet_text(items, otherwise=()):
    seqs = [x for x in items if x.seq]
    rest = [x for x in items if not x.seq]
    rest.sort(key=lambda x: not x.ext)
    parts = []
    if seqs:
        parts.append(render_seq(seqs[0].mi))
        parts += [('ELSE ' if x in otherwise else '') + render(x.mi, True) for x in rest]
    else:
        parts += [render(x.mi, False) for x in rest]
    return '; '.join(parts)


_FILL_SPECIAL = {'J27', 'CJMP', 'LC0', 'LC1', 'JSTAT', 'KSTAT', 'XSTAT', 'YSTAT'}


def fill_branch_packet(packet, nxt):
    """Move leading instructions of the fall-through block `nxt` (a machine
    instruction list, edited in place) into the closing conditional branch's
    packet as ELSE operations. They run exactly when the branch falls
    through, one packet earlier than before. Only instructions that no
    earlier `nxt` instruction constrains, and that neither read nor write a
    register the packet writes (nor write one it reads), qualify."""
    region = []
    for mi in nxt:
        if mi.op == 'macro': break
        region.append(mi)
        if mi.op in ('call', 'calli') or mi.op in TERM: break
    if not region: return set()
    nodes = [classify(mi) for mi in region]
    build_dag(nodes)
    written = set(); read = set()
    for x in packet:
        written |= x.writes; read |= x.reads | x.treads
    moved = set()
    for n in nodes:
        if n.preds or n.seq or n.mi.extra in ('flags', 'cjmp'): continue
        if n.mi.op not in ('li', 'mov', 'ialu', 'xalu', 'xsh', 'xmul', 'xmulw', 'xlsh', 'xclz', 'ld', 'st'): continue
        if n.writes & _FILL_SPECIAL or n.writes & (written | read) or (n.reads | n.treads) & written: continue
        if n.mem is not None:
            if n.mem[2]: continue   # volatile or save-area traffic keeps its place
            if any(x.mem is not None and (x.mem[0] == 'call' or 'st' in (x.mem[0], n.mem[0])) and may_alias(x, n)
                   for x in packet): continue
        if not fits(packet, n) or not packet_ok(packet_text(packet + [n], moved | {n})): continue
        packet.append(n); moved.add(n)
        written |= n.writes; read |= n.reads | n.treads
    if moved:
        ids = {id(n.mi) for n in moved}
        nxt[:] = [mi for mi in nxt if id(mi) not in ids]
    return moved


def list_schedule(nodes):
    """Cycle-driven list scheduling of a DAG built by build_dag; returns packets of nodes."""
    remaining = set(range(len(nodes)))
    cycle = 0
    packets = []
    last = nodes[-1] if nodes and (nodes[-1].mi.op in TERM or nodes[-1].mi.op in ('call', 'calli')) else None
    while remaining:
        packet = []
        progress = True
        while progress:
            progress = False
            cands = []
            for k in remaining:
                n = nodes[k]
                if n is last and len(remaining) > 1: continue
                ok = True
                for p, lat in n.preds:
                    if p.cycle is None or p.cycle + lat > cycle: ok = False; break
                if ok: cands.append(n)
            cands.sort(key=lambda n: (-n.height, n.k))
            for n in cands:
                if fits(packet, n):
                    packet.append(n); n.cycle = cycle; remaining.discard(n.k)
                    progress = True
                    break
        if not packet:
            # Should not happen; issue the oldest instruction alone.
            k = min(remaining); n = nodes[k]
            packet = [n]; n.cycle = cycle; remaining.discard(k)
        packets.append(packet)
        cycle += 1
    return packets


def schedule_region(mis, fill=None):
    assert all(mi.op not in TERM for mi in mis[:-1]), 'terminator inside a scheduling region'
    if 'sched' in opt.DISABLED:
        return [packet_text([classify(mi)]) + ';;' for mi in mis]
    nodes = [classify(mi) for mi in mis]
    build_dag(nodes)
    packets = list_schedule(nodes)
    otherwise = fill(packets) if fill else set()
    out = []
    for packet in packets:
        packet.sort(key=lambda n: n.k)
        text = packet_text(packet, otherwise)
        if otherwise and packet is packets[-1]:
            out.append(text + ';;')   # checked by fill_branch_packet
        elif packet_ok(text):
            out.append(text + ';;')
        else:
            # Model mismatch: fall back to one instruction per packet (program order).
            for n in packet:
                out.append(packet_text([n]) + ';;')
    return out
