"""Correctness-first C frontend and spill-based TS201 backend.

pycparser supplies syntax only. Type checking, word layout, ABI, selection and
runtime arithmetic are implemented here, independently of vendor tools.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import re
from pycparser import c_ast as C, c_parser
from ts201ld import make_object


class CompileError(ValueError):
    pass


def fail(node, message):
    raise CompileError(f'{getattr(node,"coord",None) or "<source>"}: {message}')


def need(test, node, message):
    if not test: fail(node, message)


def signed(v):
    v &= 0xffffffff
    return v if v < 0x80000000 else v-0x100000000


def decode_literal(value,node):
    """Decode C escapes into word32 execution characters, retaining token boundaries."""
    pattern=r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''.rstrip()
    tokens=re.findall(pattern,value)
    need(tokens and not re.sub(pattern,'',value).strip(),node,'unsupported character/string literal prefix')
    result=[]
    simple={'a':7,'b':8,'f':12,'n':10,'r':13,'t':9,'v':11,'\\':92,"'":39,'"':34,'?':63}
    for token in tokens:
        text=token[1:-1]; i=0
        while i<len(text):
            if text[i]!='\\': result.append(ord(text[i])); i+=1; continue
            i+=1; need(i<len(text),node,'unterminated escape')
            ch=text[i]; i+=1
            if ch in simple: v=simple[ch]
            elif ch in '01234567':
                digits=ch
                while i<len(text) and text[i] in '01234567' and len(digits)<3: digits+=text[i]; i+=1
                v=int(digits,8)
            elif ch=='x':
                start=i
                while i<len(text) and text[i] in '0123456789abcdefABCDEF': i+=1
                need(i>start,node,'hex escape requires digits'); v=int(text[start:i],16)
            elif ch in ('u','U'):
                length=4 if ch=='u' else 8; digits=text[i:i+length]
                need(len(digits)==length and re.fullmatch(r'[0-9a-fA-F]+',digits),node,'invalid universal character escape')
                v=int(digits,16); i+=length
                need(v<=0x10ffff and not 0xd800<=v<=0xdfff,node,'invalid universal character value')
            else: fail(node,f'unsupported escape \\{ch}')
            need(v<=0xffffffff,node,'character escape exceeds 32 bits'); result.append(v)
    return result


class Parser(c_parser.CParser):
    # pycparser normally joins literal spelling before decoding; retain the
    # boundary so "\\x1" "2" cannot accidentally turn into "\\x12".
    def _parse_unified_string_literal(self):
        token=self._expect('STRING_LITERAL')
        values=[token.value]
        while self._peek_type()=='STRING_LITERAL': values.append(self._advance().value)
        return C.Constant('string',' '.join(values),self._tok_coord(token))


def literal(node):
    value = node.value
    if node.type == 'char':
        result=decode_literal(value,node)
        need(len(result)==1,node,'multicharacter constants unsupported')
        return signed(result[0]),False
    need(node.type not in ('float','double','long double','string'), node, 'integer constant required')
    suffix = re.search(r'[uUlL]*$',value)[0].lower()
    need(suffix.count('l') <= 1, node, 'long long is not supported')
    digits = value[:len(value)-len(suffix)] if suffix else value
    try:
        v = int(digits, 16 if digits.lower().startswith('0x') else 8 if len(digits)>1 and digits[0]=='0' else 10)
    except ValueError:
        fail(node,'invalid integer literal')
    unsigned = 'u' in suffix or (v>0x7fffffff and (digits.startswith('0')))
    need(v <= (0xffffffff if unsigned else 0x7fffffff), node, 'integer literal exceeds word32 type; add U for unsigned')
    return v, unsigned


def const_int(node, constants, preprocessor=False):
    """32-bit integer constant evaluation; no host eval or floating division."""
    def calc(n):
        if isinstance(n,C.Constant): return literal(n)
        if isinstance(n,C.ID) and n.name in constants: return constants[n.name],False
        if isinstance(n,C.UnaryOp) and n.op in ('+','-','~','!'):
            v,u=calc(n.expr)
            v={'+':lambda:v,'-':lambda:-v,'~':lambda:~v,'!':lambda:int(not v)}[n.op]()
            return v & 0xffffffff, u if n.op!='!' else False
        if isinstance(n,C.TernaryOp):
            return calc(n.iftrue if calc(n.cond)[0] else n.iffalse)
        if isinstance(n,C.BinaryOp):
            a,au=calc(n.left)
            if n.op=='&&' and not a: return 0,False
            if n.op=='||' and a: return 1,False
            b,bu=calc(n.right); u=au or bu
            if n.op not in ('<<','>>'):
                a,b=(a & 0xffffffff,b & 0xffffffff) if u else (signed(a),signed(b))
            else:
                a=a & 0xffffffff if au else signed(a); b=signed(b); u=au
                need(0<=b<32,n,'shift count must be 0..31')
            if n.op in ('/','%'):
                need(b!=0,n,'division by zero in constant expression')
                q=abs(a)//abs(b)*(-1 if (a<0)!=(b<0) else 1)
                return (q if n.op=='/' else a-q*b)&0xffffffff,u
            ops={'+':lambda:a+b,'-':lambda:a-b,'*':lambda:a*b,'&':lambda:a&b,
                 '|':lambda:a|b,'^':lambda:a^b,'<<':lambda:a<<b,'>>':lambda:a>>b,
                 '==':lambda:int(a==b),'!=':lambda:int(a!=b),'<':lambda:int(a<b),
                 '<=':lambda:int(a<=b),'>':lambda:int(a>b),'>=':lambda:int(a>=b),
                 '&&':lambda:int(bool(a) and bool(b)),'||':lambda:int(bool(a) or bool(b))}
            need(n.op in ops,n,'unsupported constant operator')
            return ops[n.op]()&0xffffffff,u if n.op not in ('==','!=','<','<=','>','>=','&&','||') else False
        fail(n,'integer constant expression required')
    v,u=calc(node)
    return v if u else signed(v)


@dataclass(frozen=True)
class Type:
    kind: str
    unsigned: bool = False
    base: object = None
    count: int = 0
    name: str = ''
    params: tuple = ()
    const: bool = False
    variadic: bool = False
    unprototyped: bool = False
    volatile: bool = False


INT=Type('int')
UINT=Type('int',True)
VOID=Type('void')
BOOL=Type('bool',True)


def ptr(t): return Type('ptr',base=t)
def decay(t): return ptr(t.base) if t.kind=='array' else ptr(t) if t.kind=='function' else t
def integer(t): return t.kind in ('int','bool')
def scalar(t): return integer(t) or t.kind=='ptr'
def unqual(t): return replace(t,const=False,volatile=False)


@dataclass
class Variable:
    type: Type
    symbol: str | None = None
    offset: int | str | None = None
    register: str | None = None


class Compiler:
    def __new__(cls, filename=None, *, extended=False, optimize=1, **options):
        # -O1/-O2 use the IR backend; -O0 keeps the original spill-based code generator.
        if cls is Compiler and optimize in (1, 2):
            from .lower import Lowering
            return super().__new__(Lowering)
        return super().__new__(cls)

    def __init__(self, filename, *, extended=False, optimize=1, **options):
        if optimize not in (0,1,2): raise ValueError("optimization level must be 0, 1 or 2")
        self.optimize=optimize
        self.extended=extended
        self.filename=filename
        self.typedefs={}
        self.structs={}
        self.constants={}
        self.globals={}
        self.defined=set()
        self.exports=set()
        self.used=set()
        self.functions=[]
        self.global_inits={}
        self.data=[]
        self.code=[]
        self.serial=0
        self.scopes=[]
        self.breaks=[]
        self.continues=[]
        self.return_type=VOID
        self.labels={}
        self.gotos=set()
        # Functions that never return: _Noreturn declarations plus caller-supplied names.
        self.noreturn=set(options.get('noreturn',()))
        # Functions never inlined: __attribute__((noinline)) plus caller-supplied names.
        self.noinline=set(options.get('noinline',()))

    def label(self, prefix='L'):
        self.serial+=1
        return f'_cc_{prefix}_{self.serial}'

    def size(self,t,node=None):
        if t.kind in ('int','bool','ptr'): return 1
        if t.kind=='array':
            need(t.count>0,node,'incomplete/zero-length array')
            return t.count*self.size(t.base,node)
        if t.kind in ('struct','union'):
            need(t.name in self.structs,node,'incomplete struct')
            return (max if t.kind=='union' else sum)(self.size(ty,node) for _,ty in self.structs[t.name])
        fail(node, f'{t.kind} has no object size')

    def readonly(self,t):
        return t.const or (t.kind=='array' and self.readonly(t.base)) or (t.kind=='struct' and any(self.readonly(ty) for _,ty in self.structs[t.name]))

    def visible_constants(self):
        shadowed={name for scope in self.scopes for name in scope}
        return {name:value for name,value in self.constants.items() if name not in shadowed}

    def type(self,node):
        if isinstance(node,(C.Decl,C.Typename,C.Typedef)):
            if isinstance(node,C.Decl):
                need(node.bitsize is None,node,'bitfields unsupported')
                need(not node.align,node,'explicit alignment unsupported')
                need(not node.funcspec or set(node.funcspec)<={'inline','_Noreturn'},node,'function specifier unsupported')
            return self.type(node.type)
        if isinstance(node,C.TypeDecl):
            t=self.type(node.type)
            need(set(node.quals)<={'const','volatile','restrict'},node,'unsupported type qualifier')
            return replace(t,const='const' in node.quals or t.const,volatile='volatile' in node.quals or t.volatile)
        if isinstance(node,C.IdentifierType):
            words=node.names
            if len(words)==1 and words[0] in self.typedefs: return self.typedefs[words[0]]
            if words==['void']: return VOID
            if words==['_Bool']: return BOOL
            need(set(words)<={'signed','unsigned','char','short','int','long'} and words.count('long')<=1,
                 node,'supported scalar types are word32 char/short/int/long, _Bool and pointers; floating/long long unsupported')
            need(not ('signed' in words and 'unsigned' in words) and len(set(words))==len(words),node,'invalid integer type')
            need(not ('char' in words and any(w in words for w in ('short','int','long'))) and not ('short' in words and 'long' in words),node,'invalid integer type')
            return UINT if 'unsigned' in words else INT
        if isinstance(node,C.PtrDecl):
            need(set(node.quals)<={'const','volatile','restrict'},node,'unsupported pointer qualifier')
            base=self.type(node.type)
            need(self.extended or base.kind!='function',node,'function pointers unsupported')
            return replace(ptr(base),const='const' in node.quals,volatile='volatile' in node.quals)
        if isinstance(node,C.ArrayDecl):
            need(not node.dim_quals,node,'qualified/static array parameters unsupported')
            count=self.constant(node.dim) if node.dim is not None else 0
            need(0<=count<=0x20000,node,'invalid array size')
            base=self.type(node.type)
            need(base.kind not in ('void','function'),node,'invalid array element type')
            return Type('array',base=base,count=count)
        if isinstance(node,C.FuncDecl):
            result=self.type(node.type)
            need(scalar(result) or result.kind=='void',node,'aggregate return unsupported')
            params=[]
            need(self.extended or node.args is not None,node,'function declarations require a prototype; use (void) for no arguments')
            variadic=False
            for param in (node.args.params if node.args else []):
                if isinstance(param,C.EllipsisParam):
                    need(self.extended,param,'variadic functions unsupported')
                    variadic=True; continue
                ty=self.type(param)
                if ty.kind=='void' and len(node.args.params)==1: break
                ty=decay(ty)
                need(scalar(ty),param,'only scalar/pointer parameters supported')
                params.append(unqual(ty))
            return Type('function',base=result,params=tuple(params),variadic=variadic,unprototyped=node.args is None)
        if isinstance(node,(C.Struct,C.Union)):
            need(self.extended or isinstance(node,C.Struct),node,'unsupported type: Union')
            name=node.name or self.label('struct')
            if node.decls is not None:
                fields=[]
                for d in node.decls:
                    need(d.name and d.name not in [n for n,_ in fields],d,'anonymous/duplicate struct member unsupported')
                    t=self.type(d); self.size(t,d); fields.append((d.name,t))
                need(fields,node,'empty struct unsupported')
                need(name not in self.structs or self.structs[name]==tuple(fields),node,'struct redefinition')
                self.structs[name]=tuple(fields)
            return Type('union' if isinstance(node,C.Union) else 'struct',name=name)
        if isinstance(node,C.Enum):
            if node.values:
                value=0
                for e in node.values.enumerators:
                    if e.value is not None: value=self.constant(e.value)
                    need(e.name not in self.constants,e,'duplicate enumerator')
                    self.constants[e.name]=value; value+=1
            return INT
        fail(node,f'unsupported type: {type(node).__name__}')

    def constant(self,node):
        if isinstance(node,C.UnaryOp) and node.op=='sizeof':
            return self.size(self.type(node.expr) if isinstance(node.expr,C.Typename) else self.typeof(node.expr),node)
        if isinstance(node,C.Cast):
            t=self.type(node.to_type)
            need(integer(t) or (self.extended and t.kind=='ptr'),node,'integer cast required in constant expression')
            v=self.constant(node.expr)
            return int(v!=0) if t.kind=='bool' else v&0xffffffff if t.unsigned else signed(v)
        # Replace nested sizeof/casts through a shallow AST copy before folding.
        if isinstance(node,C.BinaryOp):
            a=self.constant(node.left)
            if node.op=='&&' and not a: return 0
            if node.op=='||' and a: return 1
            b=self.constant(node.right)
            def c(v, original):
                try: u=self.typeof(original).unsigned
                except CompileError: u=v>0x7fffffff
                return C.Constant('unsigned int' if u else 'int',str(v&0xffffffff)+'U' if u else str(v)) if v>=0 or u else C.UnaryOp('-',C.Constant('int',str(-v)))
            return const_int(C.BinaryOp(node.op,c(a,node.left),c(b,node.right)),self.visible_constants())
        return const_int(node,self.visible_constants())

    def signature(self,t,seen=frozenset()):
        if t.kind=='int': return ['int',t.unsigned]
        if t.kind in ('void','bool'): return [t.kind]
        if t.kind=='ptr': return ['ptr',self.signature(t.base,seen),t.base.const]
        if t.kind=='array': return ['array',t.count,self.signature(t.base,seen)]
        if t.kind=='function': return ['function',self.signature(t.base),[self.signature(x) for x in t.params]]
        if t.name in seen: return ['struct-ref',t.name]
        return ['struct',t.name,[[n,self.signature(ty,seen|{t.name})] for n,ty in self.structs.get(t.name,())]]

    def infer_array(self,t,init):
        if t.kind=='array' and not t.count and init is not None:
            count=len(init.exprs) if isinstance(init,C.InitList) else len(self.string(init))+1 if isinstance(init,C.Constant) and init.type=='string' else 0
            return replace(t,count=count)
        return t

    def declare_global(self,d,body=None):
        t=self.type(d)
        if not d.name: return
        need(set(d.storage)<={'static','extern'},d,'unsupported global storage class')
        name=d.name
        need(name not in self.constants,d,'declaration conflicts with enumerator')
        symbol=('_s_' if 'static' in d.storage else '_c_')+name
        if '_Noreturn' in (d.funcspec or []): self.noreturn.add(name)
        t=self.infer_array(t,d.init)
        if name in self.globals:
            old=self.globals[name]
            need(unqual(old.type)==unqual(t) or (old.type.kind==t.kind=='array' and old.type.base==t.base and not old.type.count),d,'conflicting declarations')
            need(not ('static' in d.storage and old.symbol!=symbol),d,'conflicting linkage')
            symbol=old.symbol
        self.globals[name]=Variable(t,symbol)
        definition=body is not None or (t.kind!='function' and ('extern' not in d.storage or d.init is not None))
        if definition:
            need(name not in self.defined or (body is None and d.init is None and self.global_inits.get(name) is None),d,'duplicate definition')
            self.defined.add(name)
            if not symbol.startswith('_s_'): self.exports.add(symbol)
            if body is not None: self.functions.append((d,body))
            elif name not in self.global_inits or d.init is not None: self.global_inits[name]=d.init

    def lookup(self,node):
        for scope in reversed(self.scopes):
            if node.name in scope: return scope[node.name]
        need(node.name in self.globals,node,f'undeclared identifier {node.name}')
        var=self.globals[node.name]
        self.used.add(var.symbol)
        return var

    def field(self,t,name,node):
        need(t.kind in ('struct','union') and t.name in self.structs,node,'struct member requires complete struct')
        offset=0
        for n,ty in self.structs[t.name]:
            if n==name: return replace(ty,const=ty.const or t.const),offset
            offset+=0 if t.kind=='union' else self.size(ty,node)
        fail(node,f'unknown struct member {name}')

    def common(self,a,b,node):
        a,b=decay(a),decay(b)
        if integer(a) and integer(b): return UINT if (a.kind=='int' and a.unsigned or b.kind=='int' and b.unsigned) else INT
        if a.kind==b.kind=='ptr':
            need(unqual(a.base)==unqual(b.base) or (self.extended and a.base.kind==b.base.kind=='function' and (a.base.unprototyped or b.base.unprototyped)) or a.base.kind=='void' or b.base.kind=='void',node,'incompatible pointer types')
            base=VOID if 'void' in (a.base.kind,b.base.kind) else a.base
            return ptr(replace(base,const=a.base.const or b.base.const))
        fail(node,'incompatible operand types')

    def compatible(self,to,fr,node,value=None):
        fr=decay(fr)
        if integer(to) and integer(fr): return
        if to.kind=='bool' and scalar(fr): return
        if to.kind==fr.kind=='ptr':
            need(not fr.base.const or to.base.const,node,'assignment discards const qualifier')
            self.common(to,fr,node); return
        if to.kind=='ptr' and integer(fr) and value is not None:
            try:
                if self.constant(value)==0: return
            except CompileError: pass
        if to.kind==fr.kind=='struct' and to.name==fr.name: return
        fail(node,'incompatible assignment or argument type')

    def binary_type(self,op,a,b,node):
        a,b=decay(a),decay(b)
        if op in ('&&','||'):
            need(scalar(a) and scalar(b),node,'logical operands must be scalar'); return INT
        if op in ('+','-') and (a.kind=='ptr' or b.kind=='ptr'):
            if a.kind==b.kind=='ptr':
                need(op=='-' and unqual(a.base)==unqual(b.base),node,'pointer subtraction requires compatible pointers')
                self.size(a.base,node); return INT
            pointer=a if a.kind=='ptr' else b
            other=b if a.kind=='ptr' else a
            need(integer(other) and (op=='+' or a.kind=='ptr'),node,'invalid pointer arithmetic')
            self.size(pointer.base,node); return pointer
        if op in ('==','!=','<','<=','>','>='):
            if a.kind=='ptr' and integer(b): self.compatible(a,b,node,node.right)
            elif b.kind=='ptr' and integer(a): self.compatible(b,a,node,node.left)
            else: self.common(a,b,node)
            return INT
        need(integer(a) and integer(b),node,'integer operands required')
        return (UINT if a.kind=='int' and a.unsigned else INT) if op in ('<<','>>') else self.common(a,b,node)

    def typeof(self,n):
        if isinstance(n,C.ID): return INT if n.name in self.visible_constants() else self.lookup(n).type
        if isinstance(n,C.Constant):
            if n.type=='string': return Type('array',base=INT,count=len(self.string(n))+1)
            return UINT if literal(n)[1] else INT
        if isinstance(n,C.ArrayRef):
            a,b=decay(self.typeof(n.name)),decay(self.typeof(n.subscript))
            if integer(a) and b.kind=='ptr': a,b=b,a
            need(a.kind=='ptr' and integer(b),n,'subscript requires pointer and integer')
            self.size(a.base,n); return a.base
        if isinstance(n,C.StructRef):
            t=self.typeof(n.name)
            if n.type=='->':
                t=decay(t); need(t.kind=='ptr',n,'-> requires pointer'); t=t.base
            return self.field(t,n.field.name,n)[0]
        if isinstance(n,C.UnaryOp):
            if n.op=='sizeof': return UINT
            t=self.typeof(n.expr)
            if n.op=='&':
                need(self.extended or t.kind!='function',n,'function pointers unsupported'); return ptr(t)
            t=decay(t)
            if n.op=='*':
                need(t.kind=='ptr' and t.base.kind!='void',n,'dereference requires object pointer'); return t.base
            if n.op in ('++','--','p++','p--'):
                need(scalar(t),n,'increment requires scalar'); return t
            if n.op=='!': need(scalar(t),n,'logical not requires scalar'); return INT
            need(integer(t),n,'unary operator requires integer'); return UINT if t.kind=='int' and t.unsigned else INT
        if isinstance(n,C.BinaryOp): return self.binary_type(n.op,self.typeof(n.left),self.typeof(n.right),n)
        if isinstance(n,C.Assignment):
            t=self.typeof(n.lvalue)
            if n.op=='=': self.compatible(t,self.typeof(n.rvalue),n,n.rvalue)
            else: self.compatible(t,self.binary_type(n.op[:-1],t,self.typeof(n.rvalue),n),n)
            return t
        if isinstance(n,C.Cast):
            t=self.type(n.to_type); fr=decay(self.typeof(n.expr))
            need(t.kind=='void' or scalar(t) and scalar(fr),n,'cast requires scalar types'); return t
        if isinstance(n,C.FuncCall):
            if isinstance(n.name,C.ID) and n.name.name=='__builtin_ts201_mul_q16':
                args=n.args.exprs if n.args else []
                need(len(args)==2 and all(integer(self.typeof(a)) for a in args),n,'__builtin_ts201_mul_q16 requires two integer arguments')
                return INT
            need(self.extended or isinstance(n.name,C.ID),n,'indirect function calls unsupported')
            t=self.typeof(n.name)
            if self.extended and t.kind=='ptr': t=t.base
            need(t.kind=='function',n,'called object is not a function')
            args=n.args.exprs if n.args else []
            need(t.unprototyped or len(args)==len(t.params) or (t.variadic and len(args)>=len(t.params)),n,'wrong number of arguments')
            for arg,ty in zip(args,t.params): self.compatible(ty,self.typeof(arg),arg,arg)
            return t.base
        if isinstance(n,C.TernaryOp):
            a,b=self.typeof(n.iftrue),self.typeof(n.iffalse)
            if a.kind=='ptr' and integer(b): self.compatible(a,b,n,n.iffalse); return a
            if b.kind=='ptr' and integer(a): self.compatible(b,a,n,n.iftrue); return b
            if a.kind==b.kind=='void': return VOID
            return self.common(a,b,n)
        if isinstance(n,C.ExprList): return self.typeof(n.exprs[-1])
        fail(n,f'unsupported expression {type(n).__name__}')

    def string(self,n):
        need(isinstance(n,C.Constant) and n.type=='string' and not n.value.startswith('L'),n,'ordinary string literal required')
        return decode_literal(n.value,n)

    def emit(self,text): self.code.append(text+';;')
    def mark(self,name): self.code.append(name+':')
    def jump(self,name,condition=None): self.emit((f'IF {condition}, ' if condition else '')+f'JUMP {name} (NP)')
    def slot(self,count=1):
        offset=self.next_slot; self.next_slot+=count
        need(self.next_slot<=0x10000,None,'function frame too large')
        return offset
    def save(self,reg='XR0'):
        slot=self.slot(); self.temp_slots.add(slot); self.emit(f'[J26 + {slot}] = {reg}'); return slot
    def load(self,slot,reg='XR0'): self.emit(f'{reg} = [J26 + {slot}]')

    def address_var(self,v):
        if v.symbol:
            self.used.add(v.symbol); self.emit(f'J0 = {v.symbol}')
        else: self.emit(f'J0 = J26 + {v.offset}')

    def lvalue(self,n,write=False):
        t=self.typeof(n)
        if write: need(not self.readonly(t) and t.kind not in ('array','function','void'),n,'modifiable lvalue required')
        if isinstance(n,C.ID):
            need(n.name not in self.visible_constants(),n,'enumerator is not an lvalue')
            v=self.lookup(n); need(self.extended or v.type.kind!='function',n,'function address unsupported'); self.address_var(v)
        elif isinstance(n,C.UnaryOp) and n.op=='*':
            self.expr(n.expr); self.emit('J0 = XR0')
        elif isinstance(n,C.ArrayRef):
            a,b=n.name,n.subscript
            if integer(decay(self.typeof(a))): a,b=b,a
            self.expr(a); saved=self.save(); self.expr(b)
            count=self.size(t,n)
            if count!=1:
                self.emit(f'XR1 = {count}'); self.emit('X R0 = R0 * R1 (I)')
            self.emit('J1 = XR0'); self.load(saved,'J0'); self.emit('J0 = J0 + J1')
        elif isinstance(n,C.StructRef):
            if n.type=='->': self.expr(n.name); self.emit('J0 = XR0'); base=decay(self.typeof(n.name)).base
            else: self.lvalue(n.name); base=self.typeof(n.name)
            _,offset=self.field(base,n.field.name,n)
            if offset: self.emit(f'J0 = J0 + {offset}')
        elif isinstance(n,C.Constant) and n.type=='string' and not write:
            name=self.label('string'); self.data += [name+':','.word '+', '.join(str(v) for v in self.string(n)+[0])]
            self.emit(f'J0 = {name}')
        else: fail(n,'lvalue required')
        return t

    def boolean(self):
        yes,end=self.label(),self.label()
        self.emit('J0 = XR0'); self.emit('COMP(J0, 0)'); self.jump(yes,'NJEQ')
        self.emit('XR0 = 0'); self.jump(end); self.mark(yes); self.emit('XR0 = 1'); self.mark(end)

    def convert(self,to):
        if to.kind=='bool': self.boolean()

    def branch_false(self,n,label):
        need(scalar(decay(self.typeof(n))),n,'condition requires scalar')
        self.expr(n); self.emit('J0 = XR0'); self.emit('COMP(J0, 0)'); self.jump(label,'JEQ')

    def division(self,unsigned,remainder):
        # Restoring division includes the 33rd remainder bit explicitly.
        self.emit('J0 = XR0'); self.emit('J1 = XR1')
        valid=self.label('div_valid'); self.emit('COMP(J1, 0)'); self.jump(valid,'NJEQ')
        self.emit('IDLE'); self.mark(valid)
        if not unsigned:
            self.emit('J7 = 0'); self.emit('J8 = 0')
            pos=self.label(); self.emit('COMP(J0, 0)'); self.jump(pos,'NJLT')
            self.emit('J0 = J31 - J0'); self.emit('J7 = 1'); self.emit('J8 = 1'); self.mark(pos)
            pos=self.label(); self.emit('COMP(J1, 0)'); self.jump(pos,'NJLT')
            self.emit('J1 = J31 - J1'); self.emit('J7 = J7 XOR 1'); self.mark(pos)
        self.emit('J2 = 0'); self.emit('J3 = 0'); self.emit('J4 = 32')
        loop,subtract,advance=self.label(),self.label(),self.label()
        self.mark(loop); self.emit('J5 = 0')
        pos=self.label(); self.emit('COMP(J0, 0)'); self.jump(pos,'NJLT'); self.emit('J5 = 1'); self.mark(pos)
        self.emit('J0 = J0 + J0'); self.emit('J6 = 0')
        pos=self.label(); self.emit('COMP(J3, 0)'); self.jump(pos,'NJLT'); self.emit('J6 = 1'); self.mark(pos)
        self.emit('J3 = J3 + J3'); self.emit('J3 = J3 + J5'); self.emit('J2 = J2 + J2')
        self.emit('COMP(J6, 0)'); self.jump(subtract,'NJEQ')
        self.emit('COMP(J3, J1) (U)'); self.jump(advance,'JLT')
        self.mark(subtract); self.emit('J3 = J3 - J1'); self.emit('J2 = J2 + 1')
        self.mark(advance); self.emit('J4 = J4 - 1'); self.emit('COMP(J4, 0)'); self.jump(loop,'NJEQ')
        result='J3' if remainder else 'J2'
        if not unsigned:
            positive=self.label(); self.emit(f'COMP({"J8" if remainder else "J7"}, 0)'); self.jump(positive,'JEQ')
            self.emit(f'{result} = J31 - {result}'); self.mark(positive)
        self.emit(f'XR0 = {result}')

    def binary(self,op,a,b,n):
        a,b=decay(a),decay(b)
        result=self.binary_type(op,a,b,n)
        if op in ('+','-') and (a.kind=='ptr' or b.kind=='ptr'):
            if a.kind==b.kind=='ptr':
                self.emit('X R0 = R0 - R1'); count=self.size(a.base,n)
                if count!=1: self.emit(f'XR1 = {count}'); self.division(False,False)
                return result
            p=a if a.kind=='ptr' else b
            reg='R1' if a.kind=='ptr' else 'R0'
            count=self.size(p.base,n)
            if count!=1:
                self.emit(f'XR2 = {count}'); self.emit(f'X {reg} = {reg} * R2 (I)')
        if op in ('+','-','&','|','^'):
            self.emit(f'X R0 = R0 '+{'&':'AND','|':'OR','^':'XOR'}.get(op,op)+' R1')
        elif op=='*': self.emit('X R0 = R0 * R1 (I)')
        elif op in ('/','%'): self.division(result.unsigned,op=='%')
        elif op in ('<<','>>'):
            if op=='>>': self.emit('J0 = XR1'); self.emit('J0 = J31 - J0'); self.emit('XR1 = J0')
            self.emit(f'X R0 = {"ASHIFT" if op==">>" and not result.unsigned else "LSHIFT"} R0 BY R1')
        elif op in ('==','!=','<','<=','>','>='):
            unsigned=a.kind=='ptr' or b.kind=='ptr' or (a.kind=='int' and a.unsigned) or (b.kind=='int' and b.unsigned)
            self.emit('J0 = XR0'); self.emit('J1 = XR1'); self.emit('COMP(J0, J1)'+(' (U)' if unsigned else ''))
            yes,end=self.label(),self.label()
            self.jump(yes,{'==':'JEQ','!=':'NJEQ','<':'JLT','<=':'JLE','>':'NJLE','>=':'NJLT'}[op])
            self.emit('XR0 = 0'); self.jump(end); self.mark(yes); self.emit('XR0 = 1'); self.mark(end)
        else: fail(n,f'unsupported binary operator {op}')
        return result

    def expr(self,n):
        ty=self.typeof(n)
        if isinstance(n,C.Constant) and n.type!='string': self.emit(f'XR0 = {literal(n)[0]}')
        elif isinstance(n,C.ID) and n.name in self.visible_constants(): self.emit(f'XR0 = {self.constants[n.name]}')
        elif isinstance(n,(C.ID,C.ArrayRef,C.StructRef)) or isinstance(n,C.Constant):
            self.lvalue(n)
            self.emit('XR0 = J0' if ty.kind in ('array','struct','union','function') else 'XR0 = [J0 + 0]')
        elif isinstance(n,C.BinaryOp):
            if n.op in ('&&','||'):
                short,end=self.label(),self.label()
                self.expr(n.left); self.emit('J0 = XR0'); self.emit('COMP(J0, 0)'); self.jump(short,'JEQ' if n.op=='&&' else 'NJEQ')
                self.expr(n.right); self.boolean(); self.jump(end)
                self.mark(short); self.emit(f'XR0 = {int(n.op=="||")}'); self.mark(end)
            else:
                self.expr(n.left); saved=self.save(); self.expr(n.right); self.emit('XR1 = XR0'); self.load(saved)
                self.binary(n.op,self.typeof(n.left),self.typeof(n.right),n)
        elif isinstance(n,C.Assignment):
            self.lvalue(n.lvalue,True); addr=self.save('J0')
            if ty.kind=='struct':
                need(n.op=='=',n,'struct compound assignment unsupported')
                self.expr(n.rvalue); self.emit('J1 = XR0'); self.load(addr,'J0')
                for i in range(self.size(ty,n)):
                    self.emit(f'XR0 = [J1 + {i}]'); self.emit(f'[J0 + {i}] = XR0')
                self.emit('XR0 = J0')
            else:
                if n.op!='=':
                    self.emit('XR0 = [J0 + 0]'); saved=self.save()
                self.expr(n.rvalue)
                if n.op!='=':
                    self.emit('XR1 = XR0'); self.load(saved); self.binary(n.op[:-1],ty,self.typeof(n.rvalue),n)
                self.convert(ty); self.load(addr,'J0'); self.emit('[J0 + 0] = XR0')
        elif isinstance(n,C.UnaryOp):
            op=n.op
            if op=='sizeof': self.emit(f'XR0 = {self.constant(n)}')
            elif op=='&': self.lvalue(n.expr); self.emit('XR0 = J0')
            elif op=='*':
                self.lvalue(n); self.emit('XR0 = J0' if ty.kind in ('array','struct','union','function') else 'XR0 = [J0 + 0]')
            elif op in ('++','--','p++','p--'):
                self.lvalue(n.expr,True); addr=self.save('J0'); self.emit('XR0 = [J0 + 0]'); old=self.save()
                amount=self.size(ty.base,n) if ty.kind=='ptr' else 1
                self.emit(f'XR1 = {amount}'); self.emit('X R0 = R0 '+('+' if '+' in op else '-')+' R1')
                self.convert(ty); self.load(addr,'J0'); self.emit('[J0 + 0] = XR0')
                if op.startswith('p'): self.load(old)
            else:
                self.expr(n.expr)
                if op=='-': self.emit('J0 = XR0'); self.emit('J0 = J31 - J0'); self.emit('XR0 = J0')
                elif op=='~': self.emit('XR1 = 0xffffffff'); self.emit('X R0 = R0 XOR R1')
                elif op=='!': self.boolean(); self.emit('XR1 = 1'); self.emit('X R0 = R0 XOR R1')
                elif op!='+': fail(n,f'unsupported unary operator {op}')
        elif isinstance(n,C.Cast): self.expr(n.expr); self.convert(ty)
        elif isinstance(n,C.FuncCall):
            if isinstance(n.name,C.ID) and n.name.name=='__builtin_ts201_mul_q16':
                self.expr(n.args.exprs[0]);a=self.save();self.expr(n.args.exprs[1])
                self.emit('XR1 = XR0');self.load(a)
                self.emit('X R1:0 = R0 * R1 (I)')
                self.emit('X LR1:0 = ASHIFT R1:0 BY -16')
                return INT
            ft=self.typeof(n.name)
            direct=isinstance(n.name,C.ID) and ft.kind=='function'
            if ft.kind=='ptr': ft=ft.base
            if not direct:
                self.expr(n.name); target=self.save()
            args=n.args.exprs if n.args else []
            count=(len(args)+3)&-4
            if count: self.emit(f'J27 = J27 - {count}')
            for i,arg in enumerate(args):
                param=ft.params[i] if i<len(ft.params) else decay(self.typeof(arg))
                self.expr(arg); self.convert(param); self.emit(f'[J27 + {i}] = XR0')
            if direct: self.emit(f'CALL {self.lookup(n.name).symbol} (NP)')
            else:
                self.load(target,'J0'); self.emit('J0 = J0 + 0 (CJMP)'); self.emit('CJMP_CALL (NP)(ABS)')
            if count: self.emit(f'J27 = J27 + {count}')
        elif isinstance(n,C.TernaryOp):
            other,end=self.label(),self.label(); self.branch_false(n.cond,other)
            self.expr(n.iftrue); self.jump(end); self.mark(other); self.expr(n.iffalse); self.mark(end)
        elif isinstance(n,C.ExprList):
            for e in n.exprs: self.expr(e)
        else: fail(n,f'unsupported expression {type(n).__name__}')
        return ty

    def initializer_items(self,t,init,node,base=0):
        """Yield (word offset, scalar type, initializer or None)."""
        if self.extended and isinstance(init,C.InitList) and t.kind in ('array','struct','union'):
            init=self.braced_initializer(t,init)
        if self.extended and t.kind=='union' and init is not None and not isinstance(init,C.InitList):
            init=C.InitList([init])
        if t.kind=='array':
            if isinstance(init,C.Constant) and init.type=='string':
                need(integer(t.base),init,'string initializer requires character/integer array')
                values=self.string(init)
                need(len(values)<=t.count,init,'string initializer too long')
                for i in range(t.count):
                    yield base+i,t.base,C.Constant('int',str(values[i] if i<len(values) else 0))
                return
            need(init is None or isinstance(init,C.InitList),node,'array initializer needs braces')
            values=init.exprs if init else []
            need(len(values)<=t.count,node,'too many array initializers')
            for i in range(t.count):
                value=values[i] if i<len(values) else None
                need(not isinstance(value,C.NamedInitializer),value,'designated initializers unsupported')
                yield from self.initializer_items(t.base,value,node,base+i*self.size(t.base,node))
        elif t.kind in ('struct','union'):
            need(init is None or isinstance(init,C.InitList),node,'struct initializer needs braces')
            values=init.exprs if init else []
            fields=self.structs[t.name]
            if t.kind=='union': fields=fields[:1]
            need(len(values)<=len(fields),node,'too many struct initializers')
            for i,(_,field) in enumerate(fields):
                value=values[i] if i<len(values) else None
                need(not isinstance(value,C.NamedInitializer),value,'designated initializers unsupported')
                yield from self.initializer_items(field,value,node,base)
                base+=self.size(field,node)
        else:
            self.size(t,node)
            if isinstance(init,C.InitList):
                need(len(init.exprs)==1,init,'scalar initializer requires exactly one value')
                init=init.exprs[0]
            yield base,t,init

    def braced_initializer(self,t,init):
        values=iter(init.exprs)
        pending=[]
        def get():
            return pending.pop() if pending else next(values,None)
        def consume(ty):
            v=get()
            if v is None: return None
            if ty.kind not in ('array','struct','union'): return v
            if isinstance(v,C.InitList): return self.braced_initializer(ty,v)
            if ty.kind=='array' and isinstance(v,C.Constant) and v.type=='string': return v
            pending.append(v)
            children=[ty.base]*ty.count if ty.kind=='array' else [x[1] for x in self.structs[ty.name]][:1 if ty.kind=='union' else None]
            return C.InitList([consume(child) for child in children])
        children=[t.base]*t.count if t.kind=='array' else [x[1] for x in self.structs[t.name]][:1 if t.kind=='union' else None]
        out=C.InitList([consume(child) for child in children])
        need(get() is None,init,'too many aggregate initializers')
        return out

    def static_address(self,n):
        if isinstance(n,C.ID):
            v=self.lookup(n)
            need(v.symbol is not None,n,'constant address must refer to static storage')
            return v.symbol,0,v.type
        if isinstance(n,C.ArrayRef):
            symbol,offset,t=self.static_pointer(n.name)
            return symbol,offset+self.constant(n.subscript)*self.size(t.base,n),t.base
        if isinstance(n,C.StructRef) and n.type=='.':
            symbol,offset,t=self.static_address(n.name)
            ty,field=self.field(t,n.field.name,n)
            return symbol,offset+field,ty
        fail(n,'static object address required')

    def static_pointer(self,n):
        if self.extended and isinstance(n,(C.ArrayRef,C.StructRef)):
            symbol,offset,t=self.static_address(n)
            need(t.kind=='array',n,'constant pointer requires array address')
            return symbol,offset,decay(t)
        if isinstance(n,C.Constant) and n.type=='string':
            name=self.label('string')
            self.data += [name+':','.word '+', '.join(str(v) for v in self.string(n)+[0])]
            return name,0,ptr(INT)
        if isinstance(n,C.UnaryOp) and n.op=='&':
            symbol,offset,t=self.static_address(n.expr)
            return symbol,offset,ptr(t)
        if isinstance(n,C.ID):
            symbol,offset,t=self.static_address(n)
            need(t.kind=='array' or (self.extended and t.kind=='function'),n,'pointer variable value is not a constant initializer')
            return symbol,offset,decay(t)
        if self.extended and isinstance(n,C.Cast):
            symbol,offset,t=self.static_pointer(n.expr)
            return symbol,offset,self.type(n.to_type)
        if isinstance(n,C.BinaryOp) and n.op in ('+','-'):
            symbol,offset,t=self.static_pointer(n.left)
            return symbol,offset+(1 if n.op=='+' else -1)*self.constant(n.right)*self.size(t.base,n),t
        fail(n,'constant pointer initializer required')

    def static_data(self,symbol,t,init,node):
        count=self.size(t,node)
        need(count<=0x20000,node,'object exceeds internal memory bank')
        words=[]
        if init is None:
            self.data += [symbol+':',f'.zero {count}']; return
        for _,ty,value in self.initializer_items(t,init,node):
            if value is None: words.append('0'); continue
            if ty.kind=='ptr':
                try:
                    zero=self.constant(value)
                except CompileError: zero=None
                if zero==0: words.append('0'); continue
                if self.extended and zero is not None and isinstance(value,C.Cast) and self.type(value.to_type).kind=='ptr':
                    words.append(str(zero&0xffffffff)); continue
                target,offset,fr=self.static_pointer(value)
                self.compatible(ty,fr,value,value)
                words.append(target+(f' + {offset}' if offset>=0 else f' - {-offset}'))
            else:
                self.compatible(ty,self.typeof(value),value,value)
                v=self.constant(value)
                words.append(str(int(v!=0) if ty.kind=='bool' else v&0xffffffff))
        self.data += [symbol+':','.word '+', '.join(words)]

    def local(self,d):
        if isinstance(d,C.Typedef):
            fail(d,'block-scope typedefs unsupported')
        t=self.type(d)
        if not d.name: return
        if self.extended and t.kind=='function':
            self.declare_global(d)
            return
        need(t.kind!='function',d,'block-scope function declarations unsupported')
        need(set(d.storage)<={'auto','register','static','extern'},d,'unsupported local storage')
        need(d.name not in self.scopes[-1],d,'duplicate local declaration')
        t=self.infer_array(t,d.init)
        if 'extern' in d.storage:
            need(d.init is None,d,'block extern cannot have initializer')
            if d.name not in self.globals: self.declare_global(d)
            v=self.globals[d.name]
            need(v.type==t,d,'conflicting extern declaration')
            self.scopes[-1][d.name]=v; return
        count=self.size(t,d)
        if 'static' in d.storage:
            v=Variable(t,self.label('static'))
            self.scopes[-1][d.name]=v
            self.static_data(v.symbol,t,d.init,d); return
        v=Variable(t,offset=self.slot(count))
        self.scopes[-1][d.name]=v
        if d.init is not None:
            if t.kind=='struct' and not isinstance(d.init,C.InitList):
                self.compatible(t,self.typeof(d.init),d,d.init)
                self.expr(d.init); self.emit('J1 = XR0'); self.address_var(v)
                for i in range(count):
                    self.emit(f'XR0 = [J1 + {i}]'); self.emit(f'[J0 + {i}] = XR0')
                return
            for offset,ty,value in self.initializer_items(t,d.init,d):
                if value is None: self.emit('XR0 = 0')
                else:
                    self.compatible(ty,self.typeof(value),value,value)
                    self.expr(value); self.convert(ty)
                self.emit(f'[J26 + {v.offset+offset}] = XR0')

    def statement(self,n,scope=True):
        if n is None: return
        if isinstance(n,C.Compound):
            if scope:
                saved_constants,saved_structs=self.constants.copy(),self.structs.copy()
                self.scopes.append({})
            for item in n.block_items or []: self.statement(item)
            if scope:
                self.scopes.pop()
                self.constants,self.structs=saved_constants,saved_structs
        elif isinstance(n,(C.Decl,C.Typedef)): self.local(n)
        elif isinstance(n,C.DeclList):
            for d in n.decls: self.local(d)
        elif isinstance(n,C.Return):
            if self.return_type.kind=='void': need(n.expr is None,n,'void function cannot return a value')
            else:
                need(n.expr is not None,n,'non-void return needs a value')
                self.compatible(self.return_type,self.typeof(n.expr),n,n.expr)
                self.expr(n.expr); self.convert(self.return_type)
            self.jump(self.epilogue)
        elif isinstance(n,C.If):
            other,end=self.label(),self.label(); self.branch_false(n.cond,other)
            self.statement(n.iftrue); self.jump(end); self.mark(other)
            self.statement(n.iffalse); self.mark(end)
        elif isinstance(n,(C.While,C.DoWhile,C.For)):
            start,next_,end=self.label(),self.label(),self.label()
            saved_constants,saved_structs=self.constants.copy(),self.structs.copy()
            self.scopes.append({}); self.breaks.append(end); self.continues.append(next_)
            if isinstance(n,C.For): self.statement(n.init)
            self.mark(start)
            if not isinstance(n,C.DoWhile) and n.cond is not None: self.branch_false(n.cond,end)
            self.statement(n.stmt); self.mark(next_)
            if isinstance(n,C.For) and n.next is not None: self.expr(n.next)
            if isinstance(n,C.DoWhile): self.branch_false(n.cond,end)
            self.jump(start); self.mark(end)
            self.continues.pop(); self.breaks.pop(); self.scopes.pop()
            self.constants,self.structs=saved_constants,saved_structs
        elif isinstance(n,C.Break):
            need(self.breaks,n,'break outside loop/switch'); self.jump(self.breaks[-1])
        elif isinstance(n,C.Continue):
            need(self.continues,n,'continue outside loop'); self.jump(self.continues[-1])
        elif isinstance(n,C.Switch):
            need(integer(self.typeof(n.cond)),n,'switch requires integer')
            need(isinstance(n.stmt,C.Compound),n,'switch body must be a compound statement')
            cases=[]
            def collect(item):
                if isinstance(item,(C.Case,C.Default)):
                    cases.append((item,self.label('case')))
                    for child in item.stmts or []: collect(child)
                elif isinstance(item,C.Compound):
                    for child in item.block_items or []: collect(child)
                elif isinstance(item,C.If):
                    collect(item.iftrue)
                    if item.iffalse: collect(item.iffalse)
                elif isinstance(item,(C.While,C.DoWhile,C.For)):
                    collect(item.stmt)
            for item in n.stmt.block_items or []: collect(item)
            end=self.label(); self.expr(n.cond); self.emit('J0 = XR0')
            default=end; seen=set()
            for case,label in cases:
                if isinstance(case,C.Default):
                    need(default==end,case,'duplicate default'); default=label
                else:
                    value=self.constant(case.expr)&0xffffffff
                    need(value not in seen,case,'duplicate case value'); seen.add(value)
                    self.emit(f'COMP(J0, {value})'); self.jump(label,'JEQ')
            self.jump(default)
            old=getattr(self,'case_labels',{})
            self.case_labels={id(case):label for case,label in cases}
            self.breaks.append(end); self.statement(n.stmt); self.breaks.pop()
            self.case_labels=old; self.mark(end)
        elif isinstance(n,(C.Case,C.Default)):
            need(id(n) in getattr(self,'case_labels',{}),n,'case outside supported switch')
            self.mark(self.case_labels[id(n)])
            for child in n.stmts or []: self.statement(child)
        elif isinstance(n,C.Label):
            need(n.name not in self.labels,n,'duplicate label')
            self.labels[n.name]=True; self.mark(self.function_label+'_label_'+n.name); self.statement(n.stmt)
        elif isinstance(n,C.Goto):
            self.gotos.add(n.name); self.jump(self.function_label+'_label_'+n.name)
        elif isinstance(n,C.EmptyStatement): pass
        elif isinstance(n,(C.Assignment,C.UnaryOp,C.BinaryOp,C.FuncCall,C.Cast,C.ExprList,C.TernaryOp,C.ID,C.Constant,C.ArrayRef,C.StructRef)):
            self.expr(n)
        else: fail(n,f'unsupported statement {type(n).__name__}')

    def function(self,d,body):
        f=self.globals[d.name]
        saved_constants,saved_structs=self.constants.copy(),self.structs.copy()
        start=len(self.code)
        self.next_slot=2; self.scopes=[{}]; self.labels={}; self.gotos=set()
        self.temp_slots=set()
        self.return_type=f.type.base
        self.function_label=f.symbol
        self.epilogue=self.label('return')
        parameters=d.type.args.params if d.type.args else []
        if len(parameters)==1 and isinstance(parameters[0],C.Typename) and self.type(parameters[0]).kind=='void': parameters=[]
        for i,(p,t) in enumerate(zip(parameters,f.type.params)):
            need(p.name,p,'parameter name required in function definition')
            need(p.name not in self.scopes[0],p,'duplicate parameter')
            # Preserve top-level parameter qualifiers in the function body.
            self.scopes[0][p.name]=Variable(decay(self.type(p)),offset=f'__FRAME__ + {i}')
        self.mark(f.symbol)
        self.emit('J27 = J27 - __FRAME__')
        self.emit('[J27 + 0] = J26'); self.emit('J26 = J27')
        self.emit('J0 = CJMP'); self.emit('[J26 + 1] = J0')
        self.statement(body,scope=False)
        if d.name=='main': self.emit('XR0 = 0')
        # Other non-void fall-through is C undefined behavior; deterministic zero is supplied.
        elif self.return_type.kind!='void': self.emit('XR0 = 0')
        self.mark(self.epilogue)
        self.emit('J0 = [J26 + 1]'); self.emit('J0 = J0 + 0 (CJMP)')
        self.emit('J27 = J26 + __FRAME__'); self.emit('J26 = [J26 + 0]')
        self.emit('CJMP (NP)(ABS)')
        need(self.gotos<=self.labels.keys(),d,'goto references an undefined label')
        frame=(self.next_slot+3)&-4
        self.code[start:]=[line.replace('__FRAME__',str(frame)) for line in self.code[start:]]
        self.scopes=[]
        self.constants,self.structs=saved_constants,saved_structs

    def finish(self):
        """Hook for backends that generate code after all functions are seen."""

    NOINLINE=re.compile(r'__attribute__\s*\(\s*\(\s*(?:noinline|__noinline__)\s*\)\s*\)')

    def strip_noinline(self,source):
        """Accept GCC's `__attribute__((noinline))` in a function's declaration
        specifiers: record the function (the next identifier followed by `(`)
        and remove the attribute, which the C parser does not know."""
        out=[]; pos=0
        for m in self.NOINLINE.finditer(source):
            name=re.compile(r'([A-Za-z_]\w*)\s*\(').search(source,m.end())
            if name: self.noinline.add(name[1])
            out.append(source[pos:m.start()]); pos=m.end()
        return ''.join(out)+source[pos:] if out else source

    def compile(self,source):
        source=self.strip_noinline(source)
        try: tree=Parser().parse(source,filename=self.filename)
        except Exception as e: raise CompileError(str(e)) from None
        for n in tree.ext:
            if isinstance(n,C.Typedef):
                t=self.type(n)
                need(n.name not in self.typedefs or self.typedefs[n.name]==t,n,'conflicting typedef')
                self.typedefs[n.name]=t
            elif isinstance(n,C.FuncDef):
                need(not n.param_decls,n,'old-style function definition unsupported')
                self.declare_global(n.decl,n.body)
            elif isinstance(n,C.Decl): self.declare_global(n)
            else: fail(n,f'unsupported file-scope declaration {type(n).__name__}')
        for name,init in self.global_inits.items():
            v=self.globals[name]; self.static_data(v.symbol,v.type,init,init)
        for decl,body in self.functions: self.function(decl,body)
        self.finish()
        definitions={self.globals[n].symbol for n in self.defined}
        imports={v.symbol for v in self.globals.values() if v.symbol in self.used and v.symbol not in definitions}
        need(not any(s.startswith('_s_') for s in imports),None,'referenced static function has no definition')
        lines=['.global '+name for name in sorted(self.exports)]+['.extern '+name for name in sorted(imports)]
        if self.code: lines+=['.section text, code, 4']+self.code
        if self.data: lines+=['.section data, data, 4']+self.data
        need(self.code or self.data,None,'translation unit has no definitions')
        assembly='\n'.join(lines)+'\n'
        types={v.symbol:self.signature(v.type) for v in self.globals.values() if v.symbol in self.exports | imports} if not self.extended else {}
        obj=make_object(assembly,self.filename,types)
        return obj,assembly


def compile_source(source,filename='<input>',*,includes=(),defines=(),preprocess=True,optimize=1):
    if preprocess:
        from .preprocessor import Preprocessor, PreprocessError
        try: source=Preprocessor(includes,defines).process(source,filename)
        except PreprocessError as e: raise CompileError(str(e)) from None
    return Compiler(filename,optimize=optimize).compile(source)
