"""Target-neutral three-address IR used by the optimizing backend (-O1/-O2).

Values are 32-bit virtual registers (ints). After lowering, variables may be
assigned more than once; `ssa.build` renames them into SSA form. Every block
ends in exactly one terminator: jmp, br, ret or halt.

Instruction fields:
  op   operation name (see PURE/SIDE sets below)
  d    destination vreg or None
  a    operand vregs (list)
  imm  integer immediate (constant value, memory offset, frame object)
  s    symbol name, condition code, or other string payload
  t    branch targets (list of Block)
  vol  volatile memory access / other ordering flag
"""
from __future__ import annotations

MASK = 0xffffffff

# Pure value-producing operations (candidates for folding, CSE and hoisting).
BINARY = {'add','sub','mul','and','or','xor','andn','shl','shr','sar','mulhs','mulhu','mulq16','mulfr',
          'div','udiv','mod','umod','min','max'}
UNARY = {'neg','not','clz','abs'}
PURE = BINARY | UNARY | {'const','sym','frame','copy','cmp','sel'}
COMMUTATIVE = {'add','mul','and','or','xor','mulhs','mulhu','mulq16','mulfr','min','max'}
# 'div'/'mod' trap on zero (IDLE), so they are only hoisted/removed when the
# divisor is a known non-zero constant.
TRAPPING = {'div','udiv','mod','umod'}
TERMINATORS = {'jmp','br','ret','halt','jtab','lcbr'}
CONDS = ('eq','ne','lt','le','gt','ge','ltu','leu','gtu','geu')
SWAP = {'eq':'eq','ne':'ne','lt':'gt','le':'ge','gt':'lt','ge':'le','ltu':'gtu','leu':'geu','gtu':'ltu','geu':'leu'}
NEGATE = {'eq':'ne','ne':'eq','lt':'ge','le':'gt','gt':'le','ge':'lt','ltu':'geu','leu':'gtu','gtu':'leu','geu':'ltu'}


def s32(v):
    v &= MASK
    return v - 0x100000000 if v & 0x80000000 else v


def evaluate(op, a, b=0):
    """Fold a pure integer operation on word32 values; None when not foldable."""
    a &= MASK; b &= MASK
    if op == 'add': return (a+b) & MASK
    if op == 'sub': return (a-b) & MASK
    if op == 'mul': return (a*b) & MASK
    if op == 'and': return a & b
    if op == 'or': return a | b
    if op == 'xor': return a ^ b
    if op == 'andn': return a & ~b & MASK
    if op in ('shl','shr','sar'):
        if b > 31: return None
        if op == 'shl': return (a << b) & MASK
        if op == 'shr': return a >> b
        return (s32(a) >> b) & MASK
    if op == 'mulhu': return (a*b) >> 32
    if op == 'mulhs': return ((s32(a)*s32(b)) >> 32) & MASK
    if op == 'mulq16': return ((s32(a)*s32(b)) >> 16) & MASK
    if op == 'mulfr': return ((s32(a)*s32(b)) >> 31) & MASK
    if op in ('udiv','umod'):
        if b == 0: return None
        return a//b if op == 'udiv' else a % b
    if op in ('div','mod'):
        if b == 0: return None
        x, y = s32(a), s32(b)
        q = abs(x)//abs(y) * (-1 if (x < 0) != (y < 0) else 1)
        return (q if op == 'div' else x-q*y) & MASK
    if op == 'min': return a if s32(a) < s32(b) else b
    if op == 'max': return a if s32(a) > s32(b) else b
    if op == 'neg': return (-a) & MASK
    if op == 'not': return ~a & MASK
    if op == 'abs': return abs(s32(a)) & MASK
    if op == 'clz': return 32 - a.bit_length()
    return None


def compare(cc, a, b):
    a &= MASK; b &= MASK
    if cc in ('lt','le','gt','ge'): a, b = s32(a), s32(b)
    cc = cc.rstrip('u') if cc.endswith('u') and cc != 'eq' else cc
    return {'eq':a == b,'ne':a != b,'lt':a < b,'le':a <= b,'gt':a > b,'ge':a >= b}[cc]


class Ins:
    __slots__ = ('op','d','a','imm','s','t','vol','blk')

    def __init__(self, op, d=None, a=(), imm=0, s=None, t=(), vol=False):
        self.op = op; self.d = d; self.a = list(a); self.imm = imm
        self.s = s; self.t = list(t); self.vol = vol; self.blk = None

    def __repr__(self):
        parts = [self.op]
        if self.d is not None: parts.insert(0, f'v{self.d} =')
        if self.s is not None: parts.append(str(self.s))
        if self.a: parts.append(','.join(f'v{x}' for x in self.a))
        if self.imm: parts.append(f'#{self.imm}')
        if self.t: parts.append('->'+','.join(f'B{b.id}' for b in self.t))
        if self.vol: parts.append('volatile')
        return ' '.join(parts)


class Block:
    __slots__ = ('id','ins','preds','succs','phis','depth','idom','dom_children','order','loop','freq','label')

    def __init__(self, id):
        self.id = id; self.ins = []; self.preds = []; self.succs = []; self.phis = []
        self.depth = 0; self.idom = None; self.dom_children = []; self.order = 0; self.loop = None
        self.freq = 1.0; self.label = None

    @property
    def term(self):
        return self.ins[-1] if self.ins and self.ins[-1].op in TERMINATORS else None

    def __repr__(self):
        return f'B{self.id}'


class FrameObject:
    """A stack object. kind: local, arg (incoming argument i), spill, save."""
    __slots__ = ('id','size','kind','index','offset','align')

    def __init__(self, id, size, kind='local', index=0, align=1):
        self.id = id; self.size = size; self.kind = kind; self.index = index; self.offset = None; self.align = align


class Function:
    def __init__(self, name, symbol):
        self.name = name; self.symbol = symbol
        self.blocks = []; self.entry = None
        self.nv = 0
        self.frame = []
        self.variadic = False
        self.nparams = 0
        self.returns_value = False
        self.static = symbol.startswith('_s_')
        self.address_taken = False
        self.calls = set()
        self.nblock = 0
        self.variables = set()

    def vreg(self):
        self.nv += 1
        return self.nv

    def block(self):
        self.nblock += 1
        b = Block(self.nblock)
        self.blocks.append(b)
        return b

    def frame_object(self, size, kind='local', index=0, align=1):
        f = FrameObject(len(self.frame), size, kind, index, align)
        self.frame.append(f)
        return f

    def dump(self):
        lines = [f'function {self.symbol}']
        for b in self.blocks:
            lines.append(f'  B{b.id}: preds={[p.id for p in b.preds]} depth={b.depth}')
            for p in b.phis: lines.append(f'    {p!r}')
            for i in b.ins: lines.append(f'    {i!r}')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CFG utilities

def link(fn):
    """Recompute preds/succs from terminators; drop unreachable blocks."""
    for b in fn.blocks:
        b.succs = []
        b.preds = []
    for b in fn.blocks:
        t = b.term
        assert t is not None, f'unterminated block B{b.id} in {fn.symbol}'
        if t.op == 'br' and t.t[0] is t.t[1]:
            b.ins[-1] = t = Ins('jmp', t=[t.t[0]])
        for s in t.t:
            if s not in b.succs: b.succs.append(s)
    seen = set(); stack = [fn.entry]; order = []
    while stack:
        b = stack.pop()
        if b.id in seen: continue
        seen.add(b.id); order.append(b)
        for s in reversed(b.succs): stack.append(s)
    fn.blocks = [b for b in fn.blocks if b.id in seen]
    for b in fn.blocks:
        for s in b.succs: s.preds.append(b)
    for b in fn.blocks:
        for i in b.ins: i.blk = b
        for p in b.phis:
            p.blk = b
            # Phi operands are (pred block, value) pairs kept in p.t / p.a.
            keep = [(x, v) for x, v in zip(p.t, p.a) if x in b.preds]
            p.t = [x for x, _ in keep]; p.a = [v for _, v in keep]


def rpo(fn):
    seen = set(); post = []
    stack = [(fn.entry, iter(fn.entry.succs))]
    seen.add(fn.entry.id)
    while stack:
        b, it = stack[-1]
        for s in it:
            if s.id not in seen:
                seen.add(s.id); stack.append((s, iter(s.succs))); break
        else:
            stack.pop(); post.append(b)
    post.reverse()
    for i, b in enumerate(post): b.order = i
    return post


def dominators(fn):
    order = rpo(fn)
    entry = fn.entry
    idom = {entry.id: entry}
    changed = True
    while changed:
        changed = False
        for b in order[1:]:
            new = None
            for p in b.preds:
                if p.id in idom:
                    if new is None: new = p
                    else:
                        x, y = p, new
                        while x is not y:
                            while x.order > y.order: x = idom[x.id]
                            while y.order > x.order: y = idom[y.id]
                        new = x
            if idom.get(b.id) is not new:
                idom[b.id] = new; changed = True
    for b in order:
        b.dom_children = []
    for b in order:
        b.idom = None if b is entry else idom[b.id]
        if b.idom is not None: b.idom.dom_children.append(b)
    return order


def dominates(a, b):
    while b is not None:
        if b is a: return True
        b = b.idom
    return False


def frontiers(order):
    df = {b.id: set() for b in order}
    for b in order:
        if len(b.preds) >= 2:
            for p in b.preds:
                runner = p
                while runner is not b.idom and runner is not None:
                    df[runner.id].add(b)
                    runner = runner.idom
    return df


class Loop:
    __slots__ = ('header','blocks','latches','parent','children','depth','preheader','exits')

    def __init__(self, header):
        self.header = header; self.blocks = set(); self.latches = []; self.parent = None
        self.children = []; self.depth = 1; self.preheader = None; self.exits = []


def loops(fn):
    """Natural loops (requires dominators). Sets block.depth/loop/freq; returns loops outer-first."""
    order = dominators(fn)
    by_header = {}
    for b in order:
        for s in b.succs:
            if dominates(s, b):
                lp = by_header.get(s.id)
                if lp is None:
                    lp = by_header[s.id] = Loop(s)
                lp.latches.append(b)
    for lp in by_header.values():
        lp.blocks = {lp.header.id}
        stack = [x for x in lp.latches if x is not lp.header]
        members = [lp.header]
        while stack:
            x = stack.pop()
            if x.id in lp.blocks: continue
            lp.blocks.add(x.id); members.append(x)
            stack.extend(x.preds)
    all_loops = sorted(by_header.values(), key=lambda l: len(l.blocks))
    for i, lp in enumerate(all_loops):
        for outer in all_loops[i+1:]:
            if lp.header.id in outer.blocks and outer is not lp:
                lp.parent = outer; outer.children.append(lp); break
    for b in order:
        b.depth = 0; b.loop = None
    for lp in sorted(all_loops, key=lambda l: -len(l.blocks)):
        d = 1; p = lp.parent
        while p is not None: d += 1; p = p.parent
        lp.depth = d
    ids = {b.id: b for b in order}
    for lp in sorted(all_loops, key=lambda l: len(l.blocks), reverse=True):
        for bid in lp.blocks:
            b = ids[bid]
            if lp.depth > b.depth: b.depth = lp.depth; b.loop = lp
    for b in order:
        b.freq = 8.0 ** min(b.depth, 6)
    # Blocks that cannot reach a return (e.g. calls to noreturn error handlers) are cold.
    warm = set(); stack = [b for b in order if b.term is not None and b.term.op == 'ret']
    while stack:
        b = stack.pop()
        if b.id in warm: continue
        warm.add(b.id); stack.extend(b.preds)
    if warm:
        for b in order:
            if b.id not in warm: b.freq *= 1e-3
    for lp in all_loops:
        lp.exits = [s for bid in lp.blocks for s in ids[bid].succs if s.id not in lp.blocks]
    return sorted(all_loops, key=lambda l: l.depth)


def split_edge(fn, a, b):
    """Insert a block on edge a->b and return it."""
    n = fn.block()
    n.ins.append(Ins('jmp', t=[b]))
    n.ins[0].blk = n
    t = a.term
    t.t = [n if x is b else x for x in t.t]
    for p in b.phis:
        p.t = [n if x is a else x for x in p.t]
    a.succs = [n if x is b else x for x in a.succs]
    b.preds = [n if x is a else x for x in b.preds]
    n.preds = [a]; n.succs = [b]
    n.depth = min(a.depth, b.depth); n.freq = min(a.freq, b.freq)
    return n
