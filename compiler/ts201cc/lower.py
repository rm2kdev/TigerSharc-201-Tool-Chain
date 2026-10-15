"""C AST -> IR lowering for the optimizing backend.

Type checking, layout and diagnostics are inherited from `Compiler`; this
class only replaces code generation. Conditions lower directly to branches
and loops are emitted rotated (guard + bottom test), so every loop body is
entered from a preheader that executes it at least once.
"""
from __future__ import annotations
import re
from pycparser import c_ast as C
from .compiler import (Compiler, CompileError, Variable, fail, need, literal, decay, integer, scalar,
                       ptr, INT, UINT, VOID, unqual)
from . import ir
from .ir import Ins, MASK


STANDARD_NORETURN = {'exit', 'abort', '_Exit', 'quick_exit'}


class Local:
    __slots__ = ('type', 'symbol', 'var', 'obj', 'offset', 'register')

    def __init__(self, type, var=None, obj=None):
        self.type = type; self.symbol = None; self.var = var; self.obj = obj
        self.offset = None; self.register = None


class LV:
    __slots__ = ('kind', 'var', 'base', 'off', 'ty', 'vol', 'tag')

    def __init__(self, kind, ty, var=None, base=None, off=0, vol=False, tag=None):
        self.kind = kind; self.ty = ty; self.var = var; self.base = base; self.off = off; self.vol = vol
        self.tag = tag   # restrict alias tag of the pointer the access is based on


def _written_names(body):
    """Names assigned, incremented or address-taken anywhere in body."""
    out = set()
    stack = [body]
    while stack:
        n = stack.pop()
        if n is None: continue
        e = None
        if isinstance(n, C.Assignment): e = n.lvalue
        elif isinstance(n, C.UnaryOp) and n.op in ('++', '--', 'p++', 'p--', '&'): e = n.expr
        while isinstance(e, C.Cast): e = e.expr
        if isinstance(e, C.ID): out.add(e.name)
        for _, c in n.children(): stack.append(c)
    return out


def _restrict_decl(d):
    return isinstance(d.type, C.PtrDecl) and 'restrict' in (d.type.quals or [])


def _exposed_names(body):
    """Names whose address is taken directly (&x), plus volatile declarations."""
    exposed = set(); volatile = set()

    def vol(t):
        # The declared object's own qualifiers: `volatile int *p` is an ordinary
        # pointer (to volatile data); `int *volatile p` and arrays of volatile are not.
        while isinstance(t, C.ArrayDecl): t = t.type
        return 'volatile' in (getattr(t, 'quals', None) or [])

    stack = [body]
    while stack:
        n = stack.pop()
        if n is None: continue
        if isinstance(n, C.Decl) and n.name and vol(n.type): volatile.add(n.name)
        if isinstance(n, C.UnaryOp) and n.op == '&':
            e = n.expr
            while isinstance(e, C.Cast): e = e.expr
            if isinstance(e, C.ID): exposed.add(e.name)
            elif isinstance(e, (C.ArrayRef, C.StructRef)):
                # &a[i] / &s.f expose aggregate objects, which are never promoted.
                pass
        for _, c in n.children(): stack.append(c)
    return exposed | volatile


def _sym_escapes(v, uses, seen):
    """Does a symbol address flow anywhere but load/store bases (via constant offsets)?"""
    if v in seen: return False
    seen.add(v)
    for u in uses.get(v, ()):
        if u.op == 'load' and u.a[0] == v: continue
        if u.op == 'store' and u.a[0] == v and u.a[1] != v: continue
        if u.op == 'copy' and not _sym_escapes(u.d, uses, seen): continue
        return True
    return False


class Lowering(Compiler):
    """Compiler that lowers function bodies to IR instead of assembly text."""

    def __init__(self, filename, *, extended=False, optimize=1, **options):
        super().__init__(filename, extended=extended, optimize=optimize, **options)
        self.ir_functions = []
        self._tcache = None
        self.options = options

    # -- typing cache -----------------------------------------------------
    def typeof(self, n):
        cache = self._tcache
        if cache is None: return super().typeof(n)
        key = id(n)
        t = cache.get(key)
        if t is None:
            t = super().typeof(n)
            cache[key] = t
        return t

    # -- emission helpers ---------------------------------------------------
    def new_block(self):
        return self.fn.block()

    def start(self, b):
        self.cur = b

    def emit(self, op, a=(), imm=0, s=None, vol=False, dest=True):
        i = Ins(op, self.fn.vreg() if dest else None, a, imm, s, (), vol)
        self.cur.ins.append(i)
        return i.d

    def put(self, i):
        self.cur.ins.append(i)
        return i

    def terminated(self):
        return bool(self.cur.ins) and self.cur.ins[-1].op in ir.TERMINATORS

    def jmp(self, target):
        if not self.terminated(): self.cur.ins.append(Ins('jmp', t=[target]))
        self.cur = self.new_block()  # unreachable until labelled

    def goto_block(self, target):
        """Fall into `target`, making it current."""
        if not self.terminated(): self.cur.ins.append(Ins('jmp', t=[target]))
        self.cur = target

    def br(self, cc, a, b, t, f):
        if not self.terminated(): self.cur.ins.append(Ins('br', None, [a, b], 0, cc, [t, f]))
        self.cur = self.new_block()

    def const(self, v):
        return self.emit('const', imm=v & MASK)

    def copy(self, v):
        return self.emit('copy', [v])

    def assign(self, var, v):
        self.fn.variables.add(var)
        self.cur.ins.append(Ins('copy', var, [v]))

    # -- lvalues ------------------------------------------------------------
    def lv(self, n, write=False):
        t = self.typeof(n)
        if write: need(not self.readonly(t) and t.kind not in ('array', 'function', 'void'), n, 'modifiable lvalue required')
        if isinstance(n, C.ID):
            need(n.name not in self.visible_constants(), n, 'enumerator is not an lvalue')
            v = self.lookup(n)
            need(self.extended or v.type.kind != 'function', n, 'function address unsupported')
            if isinstance(v, Local):
                if v.var is not None: return LV('var', t, var=v.var)
                return LV('mem', t, base=self.emit('frame', imm=v.obj.id), vol=t.volatile)
            self.used.add(v.symbol)
            return LV('mem', t, base=self.emit('sym', s=v.symbol), vol=t.volatile)
        if isinstance(n, C.UnaryOp) and n.op == '*':
            return LV('mem', t, base=self.rv(n.expr), vol=t.volatile, tag=self.restrict_root(n.expr))
        if isinstance(n, C.ArrayRef):
            a, b = n.name, n.subscript
            if integer(decay(self.typeof(a))): a, b = b, a
            size = self.size(t, n)
            base = self.rv(a)
            tag = self.restrict_root(a)
            try:
                k = self.constant(b)
            except CompileError:
                k = None
            if k is not None:
                # Constant subscripts fold into the access offset.
                return LV('mem', t, base=base, off=k*size, vol=t.volatile, tag=tag)
            idx = self.rv(b)
            if size != 1: idx = self.emit('mul', [idx, self.const(size)])
            return LV('mem', t, base=self.emit('add', [base, idx]), vol=t.volatile, tag=tag)
        if isinstance(n, C.StructRef):
            if n.type == '->':
                base_t = decay(self.typeof(n.name)).base
                lv = LV('mem', t, base=self.rv(n.name), tag=self.restrict_root(n.name))
            else:
                base_t = self.typeof(n.name)
                lv = self.lv(n.name)
                need(lv.kind == 'mem', n, 'lvalue required')
            _, offset = self.field(base_t, n.field.name, n)
            return LV('mem', t, base=lv.base, off=lv.off+offset, vol=t.volatile or lv.vol, tag=lv.tag)
        if isinstance(n, C.Constant) and n.type == 'string' and not write:
            name = self.label('string')
            self.data += [name+':', '.word '+', '.join(str(v) for v in self.string(n)+[0])]
            return LV('mem', t, base=self.emit('sym', s=name))
        fail(n, 'lvalue required')

    def address(self, lv):
        if lv.off == 0: return lv.base
        return self.emit('add', [lv.base, self.const(lv.off)])

    def load(self, lv):
        if lv.kind == 'var': return self.copy(lv.var)
        if lv.ty.kind in ('array', 'struct', 'union', 'function'): return self.address(lv)
        return self.emit('load', [lv.base], lv.off, s=lv.tag, vol=lv.vol)

    def store(self, lv, v):
        if lv.kind == 'var': self.assign(lv.var, v)
        else: self.cur.ins.append(Ins('store', None, [lv.base, v], lv.off, lv.tag, vol=lv.vol))

    def restrict_root(self, e):
        """Alias tag when pointer expression e is syntactically based on a restrict
        pointer declared for the whole function body and never modified.

        Accesses through two different such pointers do not overlap when either
        modifies the object (C11 6.7.3.1); the tags let alias analysis use that.
        """
        for _ in range(32):
            if isinstance(e, C.Cast): e = e.expr; continue
            if isinstance(e, C.BinaryOp) and e.op in ('+', '-'):
                sides = [x for x in ((e.left, e.right) if e.op == '+' else (e.left,))
                         if decay(self.typeof(x)).kind == 'ptr']
                if len(sides) != 1: return None
                e = sides[0]; continue
            if isinstance(e, C.UnaryOp) and e.op == '&' and isinstance(e.expr, C.ArrayRef):
                a = e.expr.name
                e = e.expr.subscript if integer(decay(self.typeof(a))) else a; continue
            break
        if not isinstance(e, C.ID): return None
        try:
            v = self.lookup(e)
        except CompileError:
            return None
        return self.restrict_tags.get(id(v))

    def copy_words(self, dst, doff, src, soff, count):
        if count <= 24:
            for i in range(count):
                v = self.emit('load', [src], soff+i)
                self.cur.ins.append(Ins('store', None, [dst, v], doff+i))
            return
        # Long copies use a counted loop.
        d0 = self.fn.vreg(); s0 = self.fn.vreg(); k = self.fn.vreg()
        self.assign(d0, self.emit('add', [dst, self.const(doff)]))
        self.assign(s0, self.emit('add', [src, self.const(soff)]))
        self.assign(k, self.const(count))
        body = self.new_block(); end = self.new_block()
        self.goto_block(body)
        v = self.emit('load', [s0], 0)
        self.cur.ins.append(Ins('store', None, [d0, v], 0))
        one = self.const(1)
        self.assign(d0, self.emit('add', [d0, one]))
        self.assign(s0, self.emit('add', [s0, one]))
        self.assign(k, self.emit('sub', [k, one]))
        self.br('ne', k, self.const(0), body, end)
        self.start(end)

    # -- conversions ----------------------------------------------------------
    def convert(self, v, to):
        if to.kind == 'bool':
            return self.emit('cmp', [v, self.const(0)], s='ne')
        return v

    # -- expressions ------------------------------------------------------------
    def rv(self, n):
        ty = self.typeof(n)
        if isinstance(n, (C.BinaryOp, C.UnaryOp, C.Cast)) and integer(ty):
            try:
                value = self.constant(n)
            except CompileError:
                pass
            else:
                return self.const(value)
        if isinstance(n, C.Constant) and n.type != 'string':
            return self.const(literal(n)[0])
        if isinstance(n, C.ID) and n.name in self.visible_constants():
            return self.const(self.constants[n.name])
        if isinstance(n, (C.ID, C.ArrayRef, C.StructRef)) or isinstance(n, C.Constant):
            return self.load(self.lv(n))
        if isinstance(n, C.BinaryOp):
            if n.op in ('&&', '||'):
                result = self.fn.vreg()
                t, f, end = self.new_block(), self.new_block(), self.new_block()
                self.cond(n, t, f)
                self.start(t); self.assign(result, self.const(1)); self.goto_block(end)
                self.cur = f; self.assign(result, self.const(0)); self.goto_block(end)
                return self.copy(result)
            a = self.rv(n.left); b = self.rv(n.right)
            return self.binary(n.op, a, b, self.typeof(n.left), self.typeof(n.right), n)
        if isinstance(n, C.Assignment):
            lv = self.lv(n.lvalue, True)
            if ty.kind in ('struct', 'union'):
                need(n.op == '=', n, 'struct compound assignment unsupported')
                src = self.rv(n.rvalue)
                dst = self.address(lv)
                self.copy_words(dst, 0, src, 0, self.size(ty, n))
                return dst
            if n.op != '=':
                old = self.load(lv)
                rhs = self.rv(n.rvalue)
                v = self.binary(n.op[:-1], old, rhs, ty, self.typeof(n.rvalue), n)
            else:
                v = self.rv(n.rvalue)
            v = self.convert(v, ty)
            self.store(lv, v)
            return v
        if isinstance(n, C.UnaryOp):
            op = n.op
            if op == 'sizeof': return self.const(self.constant(n))
            if op == '&':
                lv = self.lv(n.expr)
                need(lv.kind == 'mem', n, 'address of register variable')
                return self.address(lv)
            if op == '*':
                return self.load(self.lv(n))
            if op in ('++', '--', 'p++', 'p--'):
                lv = self.lv(n.expr, True)
                old = self.load(lv)
                amount = self.size(ty.base, n) if ty.kind == 'ptr' else 1
                new = self.emit('add' if '+' in op else 'sub', [old, self.const(amount)])
                new = self.convert(new, ty)
                self.store(lv, new)
                return old if op.startswith('p') else new
            v = self.rv(n.expr)
            if op == '-': return self.emit('neg', [v])
            if op == '~': return self.emit('not', [v])
            if op == '!': return self.emit('cmp', [v, self.const(0)], s='eq')
            if op == '+': return v
            fail(n, f'unsupported unary operator {op}')
        if isinstance(n, C.Cast):
            v = self.rv(n.expr)
            if ty.kind == 'void': return v
            return self.convert(v, ty)
        if isinstance(n, C.FuncCall):
            return self.call(n, ty)
        if isinstance(n, C.TernaryOp):
            other, end, yes = self.new_block(), self.new_block(), self.new_block()
            result = self.fn.vreg()
            self.cond(n.cond, yes, other)
            self.start(yes)
            v = self.rv(n.iftrue)
            if ty.kind != 'void': self.assign(result, v)
            self.goto_block(end)
            self.cur = other
            v = self.rv(n.iffalse)
            if ty.kind != 'void': self.assign(result, v)
            self.goto_block(end)
            return self.copy(result) if ty.kind != 'void' else self.const(0)
        if isinstance(n, C.ExprList):
            v = None
            for e in n.exprs: v = self.rv(e)
            return v
        fail(n, f'unsupported expression {type(n).__name__}')

    def binary(self, op, a, b, at, bt, n):
        at, bt = decay(at), decay(bt)
        result = self.binary_type(op, at, bt, n)
        if op in ('+', '-') and (at.kind == 'ptr' or bt.kind == 'ptr'):
            if at.kind == bt.kind == 'ptr':
                diff = self.emit('sub', [a, b])
                size = self.size(at.base, n)
                if size == 1: return diff
                return self.emit('exactdiv', [diff], size)
            if at.kind == 'ptr':
                size = self.size(at.base, n)
                if size != 1: b = self.emit('mul', [b, self.const(size)])
            else:
                size = self.size(bt.base, n)
                if size != 1: a = self.emit('mul', [a, self.const(size)])
            return self.emit('add' if op == '+' else 'sub', [a, b])
        simple = {'+': 'add', '-': 'sub', '*': 'mul', '&': 'and', '|': 'or', '^': 'xor', '<<': 'shl'}
        if op in simple: return self.emit(simple[op], [a, b])
        if op == '>>': return self.emit('shr' if result.unsigned else 'sar', [a, b])
        if op == '/': return self.emit('udiv' if result.unsigned else 'div', [a, b])
        if op == '%': return self.emit('umod' if result.unsigned else 'mod', [a, b])
        if op in ('==', '!=', '<', '<=', '>', '>='):
            unsigned = at.kind == 'ptr' or bt.kind == 'ptr' or (at.kind == 'int' and at.unsigned) or (bt.kind == 'int' and bt.unsigned)
            return self.emit('cmp', [a, b], s=self.cc(op, unsigned))
        fail(n, f'unsupported binary operator {op}')

    @staticmethod
    def cc(op, unsigned):
        base = {'==': 'eq', '!=': 'ne', '<': 'lt', '<=': 'le', '>': 'gt', '>=': 'ge'}[op]
        return base+'u' if unsigned and base not in ('eq', 'ne') else base

    def call(self, n, ty):
        if isinstance(n.name, C.ID) and n.name.name == '__builtin_ts201_mul_q16':
            a = self.rv(n.args.exprs[0]); b = self.rv(n.args.exprs[1])
            return self.emit('mulq16', [a, b])
        ft = self.typeof(n.name)
        direct = isinstance(n.name, C.ID) and ft.kind == 'function'
        if ft.kind == 'ptr': ft = ft.base
        target = None
        if not direct: target = self.rv(n.name)
        args = n.args.exprs if n.args else []
        values = []
        for i, arg in enumerate(args):
            param = ft.params[i] if i < len(ft.params) else decay(self.typeof(arg))
            values.append(self.convert(self.rv(arg), param))
        returns = ft.base.kind != 'void'
        if direct and len(values) == 3 and n.name.name in ('memcpy', 'memset') and n.name.name not in self.defined                 and not self.options.get('no_builtin'):
            return self.builtin_memory(n.name.name, values)
        if direct and len(values) == 1 and n.name.name in ('toupper', 'tolower') and n.name.name not in self.defined                 and not self.options.get('no_builtin'):
            # C-locale case mapping: c - 'a' <= 25 (unsigned) selects the letters.
            c = values[0]
            lo = 97 if n.name.name == 'toupper' else 65
            t = self.emit('sub', [c, self.const(lo)])
            mapped = self.emit('sub' if lo == 97 else 'add', [c, self.const(32)])
            # sel(cc, x, y, a, b) = (x cc y) ? a : b
            return self.emit('sel', [t, self.const(25), mapped, c], s='leu')
        noreturn = False
        if direct:
            symbol = self.lookup(n.name).symbol
            name = n.name.name
            # Standard library functions that never return, when only declared here.
            noreturn = name in self.noreturn or (name in STANDARD_NORETURN and name not in self.defined)
            i = Ins('call', self.fn.vreg() if returns else None, values, len(values), symbol, vol=noreturn)
            self.fn.calls.add(symbol)
        else:
            i = Ins('call', self.fn.vreg() if returns else None, [target]+values, len(values), None)
        self.cur.ins.append(i)
        result = i.d if returns else self.const(0)
        if noreturn:
            # Control never comes back: the rest of this block is unreachable.
            self.cur.ins.append(Ins('halt'))
            self.cur = self.new_block()
            result = self.const(0) if returns else result
        return result

    def builtin_memory(self, name, values):
        """memcpy/memset of `n` words as an ordinary IR loop (then optimized like any loop)."""
        d, x, n = values
        self.serial += 1
        tag = f'restrict:{self.serial}'
        const = None
        try:
            ni = self.cur.ins[-1] if self.cur.ins and self.cur.ins[-1].d == n and self.cur.ins[-1].op == 'const' else None
            const = ni.imm if ni is not None else None
        except IndexError:
            pass
        if const is not None and const <= 16:
            # 's' tags: valid within one dynamic call only (see AliasInfo.may_alias).
            tag = f'restrict:s{self.serial}'
            for k in range(const):
                v = self.emit('load', [x], k, s=tag+':src') if name == 'memcpy' else x
                self.cur.ins.append(Ins('store', None, [d, v], k, tag+':dst'))
            return d
        dv, sv, nv = self.fn.vreg(), self.fn.vreg(), self.fn.vreg()
        self.assign(dv, d); self.assign(sv, x); self.assign(nv, n)
        if name == 'memcpy':
            # n >= 16: copy 0..3 words until the destination is quad aligned,
            # then four words per transfer: a Q loop when the source is aligned
            # too, otherwise the data-alignment-buffer copy `qcopy` (DAB quad
            # loads; disabled by the `no_dab` option). 0..3 words remain.
            big, words, done = self.new_block(), self.new_block(), self.new_block()
            self.br('ltu', nv, self.const(16), words, big)
            self.start(big)
            hv = self.emit('and', [self.emit('sub', [self.const(0), dv]), self.const(3)])
            self.copy_bits(tag, dv, sv, hv)
            self.assign(nv, self.emit('sub', [nv, hv]))
            qv = self.fn.vreg()
            self.assign(qv, self.emit('shr', [nv, self.const(2)]))
            quad, misaligned, after = self.new_block(), self.new_block(), self.new_block()
            self.br('ne', self.emit('and', [sv, self.const(3)]), self.const(0), misaligned, quad)
            self.start(misaligned)
            if self.options.get('no_dab'):
                self.goto_block(words)
            else:
                # qv >= 3: qcopy moves 4*(2p+1+odd) words, p = (qv-1)>>1 >= 1.
                q1 = self.emit('sub', [qv, self.const(1)])
                pv = self.emit('shr', [q1, self.const(1)])
                ov = self.emit('and', [q1, self.const(1)])
                self.cur.ins.append(Ins('qcopy', None, [dv, sv, pv, ov], 0, tag))
                moved = self.emit('shl', [qv, self.const(2)])
                self.assign(dv, self.emit('add', [dv, moved]))
                self.assign(sv, self.emit('add', [sv, moved]))
                self.goto_block(after)
            self.start(quad)
            qloop = self.new_block()
            self.goto_block(qloop)
            q4 = self.emit('loadq', [sv], 0, s=tag+':src')
            self.cur.ins.append(Ins('storeq', None, [dv, q4], 0, tag+':dst'))
            four = self.const(4)
            self.assign(dv, self.emit('add', [dv, four]))
            self.assign(sv, self.emit('add', [sv, four]))
            self.assign(qv, self.emit('sub', [qv, self.const(1)]))
            self.br('ne', qv, self.const(0), qloop, after)
            self.start(after)
            self.copy_bits(tag, dv, sv, self.emit('and', [nv, self.const(3)]))
            self.goto_block(done)
            self.start(words)
            self.words_loop(tag, dv, sv, nv, name)
            self.goto_block(done)
            self.start(done)
            return d
        # memset, n >= 16: 0..3 words align the destination; the first aligned
        # quad is stored as words and read back as the quad value; then one Q
        # store per four words; 0..3 words remain.
        big, words, done = self.new_block(), self.new_block(), self.new_block()
        self.br('ltu', nv, self.const(16), words, big)
        self.start(big)
        hv = self.emit('and', [self.emit('sub', [self.const(0), dv]), self.const(3)])
        self.copy_bits(tag, dv, sv, hv, fill=True)
        for k in range(4):
            self.cur.ins.append(Ins('store', None, [dv, sv], k, tag+':dst'))
        q4 = self.emit('loadq', [dv], 0, s=tag+':dst')
        four = self.const(4)
        self.assign(dv, self.emit('add', [dv, four]))
        self.assign(nv, self.emit('sub', [self.emit('sub', [nv, hv]), four]))
        qv = self.fn.vreg()
        self.assign(qv, self.emit('shr', [nv, self.const(2)]))
        # The tail address is formed before the loop, so the loop pointer dies in it.
        ev = self.fn.vreg()
        self.assign(ev, self.emit('add', [dv, self.emit('andn', [nv, self.const(3)])]))
        qloop, after = self.new_block(), self.new_block()
        self.goto_block(qloop)
        self.cur.ins.append(Ins('storeq', None, [dv, q4], 0, tag+':dst'))
        self.assign(dv, self.emit('add', [dv, self.const(4)]))
        self.assign(qv, self.emit('sub', [qv, self.const(1)]))
        self.br('ne', qv, self.const(0), qloop, after)
        self.start(after)
        self.copy_bits(tag, ev, sv, self.emit('and', [nv, self.const(3)]), fill=True)
        self.goto_block(done)
        self.start(words)
        self.words_loop(tag, dv, sv, nv, name)
        self.goto_block(done)
        self.start(done)
        return d

    def words_loop(self, tag, dv, sv, nv, name):
        """while (nv) { *dv++ = memcpy ? *sv++ : sv; nv--; }"""
        body, end = self.new_block(), self.new_block()
        self.br('eq', nv, self.const(0), end, body)
        self.start(body)
        v = self.emit('load', [sv], 0, s=tag+':src') if name == 'memcpy' else sv
        self.cur.ins.append(Ins('store', None, [dv, v], 0, tag+':dst'))
        one = self.const(1)
        self.assign(dv, self.emit('add', [dv, one]))
        if name == 'memcpy': self.assign(sv, self.emit('add', [sv, one]))
        self.assign(nv, self.emit('sub', [nv, one]))
        self.br('ne', nv, self.const(0), body, end)
        self.start(end)

    def copy_bits(self, tag, dv, sv, kv, fill=False):
        """Copy kv (0..3) words straight-line: bit 1 moves two, bit 0 one.
        fill: store the value sv instead (memset)."""
        for bit in (2, 1):
            yes, no = self.new_block(), self.new_block()
            self.br('eq', self.emit('and', [kv, self.const(bit)]), self.const(0), no, yes)
            self.start(yes)
            for k in range(bit):
                v = sv if fill else self.emit('load', [sv], k, s=tag+':src')
                self.cur.ins.append(Ins('store', None, [dv, v], k, tag+':dst'))
            self.assign(dv, self.emit('add', [dv, self.const(bit)]))
            if not fill: self.assign(sv, self.emit('add', [sv, self.const(bit)]))
            self.goto_block(no)

    # -- conditions ---------------------------------------------------------------
    def cond(self, n, t, f):
        """Branch to t when n is true, else f. Leaves self.cur unreachable."""
        ty = self.typeof(n)
        need(scalar(decay(ty)), n, 'condition requires scalar')
        try:
            if integer(ty) and not isinstance(n, (C.ID,)) or isinstance(n, C.Constant):
                value = self.constant(n)
                self.jmp(t if value else f)
                return
        except CompileError:
            pass
        if isinstance(n, C.BinaryOp) and n.op == '&&':
            mid = self.new_block()
            self.cond(n.left, mid, f); self.start(mid); self.cond(n.right, t, f); return
        if isinstance(n, C.BinaryOp) and n.op == '||':
            mid = self.new_block()
            self.cond(n.left, t, mid); self.start(mid); self.cond(n.right, t, f); return
        if isinstance(n, C.UnaryOp) and n.op == '!':
            self.cond(n.expr, f, t); return
        if isinstance(n, C.BinaryOp) and n.op in ('==', '!=', '<', '<=', '>', '>='):
            a = self.rv(n.left); b = self.rv(n.right)
            at, bt = decay(self.typeof(n.left)), decay(self.typeof(n.right))
            # _Bool is Type('bool', unsigned=True) but promotes to int: compare signed.
            unsigned = at.kind == 'ptr' or bt.kind == 'ptr' or (at.kind == 'int' and at.unsigned) or (bt.kind == 'int' and bt.unsigned)
            self.br(self.cc(n.op, unsigned), a, b, t, f)
            return
        if isinstance(n, C.ExprList):
            for e in n.exprs[:-1]: self.rv(e)
            self.cond(n.exprs[-1], t, f); return
        v = self.rv(n)
        self.br('ne', v, self.const(0), t, f)

    # -- declarations ---------------------------------------------------------------
    def local(self, d):
        if isinstance(d, C.Typedef):
            fail(d, 'block-scope typedefs unsupported')
        t = self.type(d)
        if not d.name: return
        if self.extended and t.kind == 'function':
            self.declare_global(d)
            return
        need(t.kind != 'function', d, 'block-scope function declarations unsupported')
        need(set(d.storage) <= {'auto', 'register', 'static', 'extern'}, d, 'unsupported local storage')
        need(d.name not in self.scopes[-1], d, 'duplicate local declaration')
        t = self.infer_array(t, d.init)
        if 'extern' in d.storage:
            need(d.init is None, d, 'block extern cannot have initializer')
            if d.name not in self.globals: self.declare_global(d)
            v = self.globals[d.name]
            need(v.type == t, d, 'conflicting extern declaration')
            self.scopes[-1][d.name] = v; return
        count = self.size(t, d)
        if 'static' in d.storage:
            v = Variable(t, self.label('static'))
            self.scopes[-1][d.name] = v
            self.static_data(v.symbol, t, d.init, d); return
        if d.name in self.promotable and scalar(t) and not t.volatile:
            v = Local(t, var=self.fn.vreg())
            # A read before any assignment then renames to SSA's undefined value.
            self.fn.variables.add(v.var)
        else:
            v = Local(t, obj=self.fn.frame_object(count))
        if len(self.scopes) == 1 and _restrict_decl(d) and v.var is not None and d.name not in self.restrict_written \
                and d.init is not None and not isinstance(d.init, C.InitList):
            # A function-scope restrict local initialised from a restrict pointer
            # shares that pointer's tag; from a non-parameter local it is untracked.
            src = self.restrict_root(d.init)
            root = d.init
            while isinstance(root, C.Cast): root = root.expr
            local_ptr = isinstance(root, C.ID) and root.name not in self.param_names \
                and isinstance(self.scopes[0].get(root.name), Local)
            if src is not None: self.restrict_tags[id(v)] = src
            elif not local_ptr: self.restrict_tags[id(v)] = f'{self.restrict_scope}:{d.name}'
        self.scopes[-1][d.name] = v
        if d.init is None: return
        if t.kind in ('struct', 'union') and not isinstance(d.init, C.InitList):
            self.compatible(t, self.typeof(d.init), d, d.init)
            src = self.rv(d.init)
            dst = self.emit('frame', imm=v.obj.id)
            self.copy_words(dst, 0, src, 0, count)
            return
        base = None
        for offset, ty, value in self.initializer_items(t, d.init, d):
            if value is None: x = self.const(0)
            else:
                self.compatible(ty, self.typeof(value), value, value)
                x = self.convert(self.rv(value), ty)
            if v.var is not None: self.assign(v.var, x)
            else:
                if base is None: base = self.emit('frame', imm=v.obj.id)
                self.cur.ins.append(Ins('store', None, [base, x], offset, vol=t.volatile))

    # -- statements -------------------------------------------------------------------
    def statement(self, n, scope=True):
        if n is None: return
        if isinstance(n, C.Compound):
            if scope:
                saved_constants, saved_structs = self.constants.copy(), self.structs.copy()
                self.scopes.append({})
            for item in n.block_items or []: self.statement(item)
            if scope:
                self.scopes.pop()
                self.constants, self.structs = saved_constants, saved_structs
        elif isinstance(n, (C.Decl, C.Typedef)): self.local(n)
        elif isinstance(n, C.DeclList):
            for d in n.decls: self.local(d)
        elif isinstance(n, C.Return):
            if self.return_type.kind == 'void':
                need(n.expr is None, n, 'void function cannot return a value')
                self.cur.ins.append(Ins('ret'))
            else:
                need(n.expr is not None, n, 'non-void return needs a value')
                self.compatible(self.return_type, self.typeof(n.expr), n, n.expr)
                v = self.convert(self.rv(n.expr), self.return_type)
                self.cur.ins.append(Ins('ret', None, [v]))
            self.cur = self.new_block()
        elif isinstance(n, C.If):
            yes, other, end = self.new_block(), self.new_block(), self.new_block()
            self.cond(n.cond, yes, other if n.iffalse else end)
            self.start(yes); self.statement(n.iftrue); self.goto_block(end if not n.iffalse else end)
            if n.iffalse:
                self.cur = other; self.statement(n.iffalse); self.goto_block(end)
            else:
                self.cur = end
        elif isinstance(n, (C.While, C.DoWhile, C.For)):
            body, test, end = self.new_block(), self.new_block(), self.new_block()
            nxt = self.new_block() if isinstance(n, C.For) else test
            saved_constants, saved_structs = self.constants.copy(), self.structs.copy()
            self.scopes.append({}); self.breaks.append(end); self.continues.append(nxt)
            if isinstance(n, C.For): self.statement(n.init)
            cond = n.cond
            if isinstance(n, C.DoWhile):
                self.goto_block(body)
            elif cond is not None:
                self.cond(cond, body, end)
            else:
                self.jmp(body)
            self.start(body)
            self.statement(n.stmt)
            if isinstance(n, C.For):
                self.goto_block(nxt)
                if n.next is not None: self.rv(n.next)
            self.goto_block(test)
            if cond is not None: self.cond(cond, body, end)
            else: self.jmp(body)
            self.start(end)
            self.continues.pop(); self.breaks.pop(); self.scopes.pop()
            self.constants, self.structs = saved_constants, saved_structs
        elif isinstance(n, C.Break):
            need(self.breaks, n, 'break outside loop/switch'); self.jmp(self.breaks[-1])
        elif isinstance(n, C.Continue):
            need(self.continues, n, 'continue outside loop'); self.jmp(self.continues[-1])
        elif isinstance(n, C.Switch):
            self.switch(n)
        elif isinstance(n, (C.Case, C.Default)):
            need(id(n) in self.case_blocks, n, 'case outside supported switch')
            self.goto_block(self.case_blocks[id(n)])
            for child in n.stmts or []: self.statement(child)
        elif isinstance(n, C.Label):
            need(n.name not in self.labels, n, 'duplicate label')
            self.labels[n.name] = True
            self.goto_block(self.label_block(n.name))
            self.statement(n.stmt)
        elif isinstance(n, C.Goto):
            self.gotos.add(n.name); self.jmp(self.label_block(n.name))
        elif isinstance(n, C.EmptyStatement): pass
        elif isinstance(n, (C.Assignment, C.UnaryOp, C.BinaryOp, C.FuncCall, C.Cast, C.ExprList, C.TernaryOp, C.ID, C.Constant, C.ArrayRef, C.StructRef)):
            self.rv(n)
        else: fail(n, f'unsupported statement {type(n).__name__}')

    def label_block(self, name):
        b = self.label_blocks.get(name)
        if b is None: b = self.label_blocks[name] = self.new_block()
        return b

    def switch(self, n):
        need(integer(self.typeof(n.cond)), n, 'switch requires integer')
        need(isinstance(n.stmt, C.Compound), n, 'switch body must be a compound statement')
        cases = []

        def collect(item):
            if isinstance(item, (C.Case, C.Default)):
                cases.append(item)
                for child in item.stmts or []: collect(child)
            elif isinstance(item, C.Compound):
                for child in item.block_items or []: collect(child)
            elif isinstance(item, C.If):
                collect(item.iftrue)
                if item.iffalse: collect(item.iffalse)
            elif isinstance(item, (C.While, C.DoWhile, C.For)):
                collect(item.stmt)
        for item in n.stmt.block_items or []: collect(item)
        end = self.new_block()
        v = self.rv(n.cond)
        default = end; seen = {}
        blocks = {}
        for case in cases:
            blocks[id(case)] = self.new_block()
            if isinstance(case, C.Default):
                need(default is end, case, 'duplicate default'); default = blocks[id(case)]
            else:
                value = self.constant(case.expr) & MASK
                need(value not in seen, case, 'duplicate case value'); seen[value] = blocks[id(case)]
        self.cur.ins.append(Ins('switch', None, [v], 0, None, [default]+list(seen.values())))
        self.cur.ins[-1].s = list(seen.keys())
        self.lower_switch(self.cur.ins.pop())
        old = self.case_blocks
        self.case_blocks = dict(old); self.case_blocks.update(blocks)
        self.breaks.append(end); self.statement(n.stmt); self.breaks.pop()
        self.case_blocks = old
        self.goto_block(end)

    def lower_switch(self, sw):
        """Compare tree (binary search on sorted cases) or a jump table when dense."""
        v = sw.a[0]; default = sw.t[0]
        pairs = sorted(zip(sw.s, sw.t[1:]), key=lambda p: ir.s32(p[0]))
        if not pairs:
            self.jmp(default); return
        lo, hi = ir.s32(pairs[0][0]), ir.s32(pairs[-1][0])
        span = hi-lo+1
        if len(pairs) >= 5 and span <= 3*len(pairs)+2 and span <= 1024:
            table = {ir.s32(k): b for k, b in pairs}
            index = self.emit('sub', [v, self.const(lo)]) if lo else v
            inrange = self.new_block()
            self.br('leu', index, self.const(span-1), inrange, default)
            self.start(inrange)
            targets = [table.get(lo+i, default) for i in range(span)]
            self.cur.ins.append(Ins('jtab', None, [index], 0, None, targets))
            self.cur = self.new_block()
            return

        def tree(items):
            if len(items) <= 3:
                for value, b in items:
                    nxt = self.new_block()
                    self.br('eq', v, self.const(value), b, nxt)
                    self.start(nxt)
                self.jmp(default)
                return
            mid = len(items)//2
            left, right = self.new_block(), self.new_block()
            self.br('lt', v, self.const(items[mid][0]), left, right)
            self.start(left); tree(items[:mid])
            self.start(right); tree(items[mid:])
        tree(pairs)

    # -- functions ------------------------------------------------------------------
    def function(self, d, body):
        import os
        if d.name in set(os.environ.get('TS201_NOOPT_FUNCS', '').split(',')):
            # Debugging aid: emit this function with the original -O0 generator,
            # sharing declarations, data and label numbering with this unit.
            plain = object.__new__(Compiler)
            plain.__dict__.update(self.__dict__)
            plain.optimize = 0; plain.code = []
            Compiler.function(plain, d, body)
            self.code.extend(plain.code); self.serial = plain.serial
            return
        f = self.globals[d.name]
        saved_constants, saved_structs = self.constants.copy(), self.structs.copy()
        self._tcache = {}
        self.fn = fn = ir.Function(d.name, f.symbol)
        fn.variadic = f.type.variadic or f.type.unprototyped
        self.scopes = [{}]; self.labels = {}; self.gotos = set(); self.label_blocks = {}
        self.case_blocks = {}; self.breaks = []; self.continues = []
        self.promotable = self.promotable_names(body, d)
        self.restrict_tags = {}
        self.restrict_written = _written_names(body)
        self.serial += 1
        self.restrict_scope = f'restrict:p{self.serial}'
        self.return_type = f.type.base
        fn.returns_value = self.return_type.kind != 'void'
        self.function_label = f.symbol
        fn.entry = self.cur = self.new_block()
        parameters = d.type.args.params if d.type.args else []
        if len(parameters) == 1 and isinstance(parameters[0], C.Typename) and self.type(parameters[0]).kind == 'void':
            parameters = []
        fn.nparams = len(parameters)
        self.param_names = {getattr(p, 'name', None) for p in parameters}
        for i, (p, t) in enumerate(zip(parameters, f.type.params)):
            need(p.name, p, 'parameter name required in function definition')
            need(p.name not in self.scopes[0], p, 'duplicate parameter')
            ty = decay(self.type(p))
            obj = fn.frame_object(1, 'arg', i)
            if p.name in self.promotable and scalar(ty) and not ty.volatile:
                v = Local(ty, var=fn.vreg())
                base = self.emit('frame', imm=obj.id)
                self.assign(v.var, self.emit('load', [base], 0))
            else:
                v = Local(ty, obj=obj)
            self.scopes[0][p.name] = v
            if _restrict_decl(p) and v.var is not None and p.name not in self.restrict_written:
                self.restrict_tags[id(v)] = f'{self.restrict_scope}:{p.name}'
        self.statement(body, scope=False)
        if not self.terminated():
            if fn.returns_value or d.name == 'main':
                self.cur.ins.append(Ins('ret', None, [self.const(0)]))
            else:
                self.cur.ins.append(Ins('ret'))
        need(self.gotos <= self.labels.keys(), d, 'goto references an undefined label')
        # Close any dangling unreachable blocks.
        for b in fn.blocks:
            if not b.ins or b.ins[-1].op not in ir.TERMINATORS:
                b.ins.append(Ins('ret', None, [self.const_in(b, 0)]) if fn.returns_value else Ins('ret'))
        ir.link(fn)
        self.ir_functions.append(fn)
        self.scopes = []; self._tcache = None
        self.constants, self.structs = saved_constants, saved_structs

    def const_in(self, b, v):
        i = Ins('const', self.fn.vreg(), (), v)
        b.ins.append(i)
        return i.d

    def promotable_names(self, body, d):
        names = set()
        stack = [body, d.type]
        while stack:
            n = stack.pop()
            if n is None: continue
            if isinstance(n, (C.Decl, C.Typename)) and n.name: names.add(n.name)
            for _, c in n.children(): stack.append(c)
        return names - _exposed_names(body) - _exposed_names(d.type)

    def finish(self):
        from .backend import generate
        self.options = dict(self.options)
        self.options['unaliased_globals'] = self.unaliased_globals()
        self.code.extend(generate(self, self.ir_functions))
        # Imports are the symbols the emitted code and data really reference
        # (type checking alone, builtin expansion or inlining leave none).
        tokens = set()
        for line in self.code:
            tokens.update(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', line))
        for line in self.data:
            tokens.update(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', line))
        self.used &= tokens

    def unaliased_globals(self):
        """Scalar globals no pointer can reach: their address never escapes.

        Static objects are decided from this unit alone. External objects need
        the whole-program `address_taken` option (names whose address is taken
        anywhere, including assembly); without it they are assumed reachable.
        """
        escaped = set()
        for fn in self.ir_functions:
            uses = {}
            for b in fn.blocks:
                for i in b.phis + b.ins:
                    for x in i.a: uses.setdefault(x, []).append(i)
            for b in fn.blocks:
                for i in b.ins:
                    if i.op == 'sym' and _sym_escapes(i.d, uses, set()): escaped.add(i.s)
        for line in self.data:
            if line.startswith('.word '):
                escaped.update(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', line[6:]))
        taken = self.options.get('address_taken')
        out = set()
        for name, v in self.globals.items():
            if v.type.kind not in ('int', 'bool', 'ptr') or v.symbol in escaped: continue
            if v.symbol.startswith('_s_') or (taken is not None and name not in taken):
                out.add(v.symbol)
        return frozenset(out)
