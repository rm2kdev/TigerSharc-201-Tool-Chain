"""Compute encodings derived from TS201 PR Rev 1.1, Appendix C, C-3..25.

Explicit X/Y/XY selection; no external runtime imports or raw instruction escape.
An operation is accepted only when its operand widths and options are checked.
"""
import re
from dataclasses import dataclass

R = r'[FLSBQ]?R(?:[12]?\d|3[01])(?::(?:[12]?\d|3[01]))?'


@dataclass(frozen=True)
class Register:
    index: int
    count: int = 1
    kind: str = ''


# Operations written without X/Y/XY that belong to the compute blocks: a
# compute register/accumulator/trellis destination or a compute-only keyword.
UNPREFIXED = re.compile(r'(?:[FLSBQ]?R\d|[SL]?MR[0-4]|PR[01]|S?TR\d|THR\d|CMCTL\s*=|BFOTMP\s*=|'
                        r'(?:[FLSB]?COMP|BITEST|BKFPT)\b)', re.I)


def nf_exempt(word):
    """Forms that take no (NF) and always update status (PR 3-13 ALU: PASS,
    COMP, FCOMP; 4-43 CLU: TR/THR/CMCTL transfers; 5-22 multiplier: MR4
    transfers; 6-19 shifter: BITEST, BKFPT, X/YSTAT(L), TR/THR/CMCTL loads)."""
    if (word>>28)&3==3:
        return (word>>20)&63==0                     # C-12: Rs = TR/THR/CMCTL
    cu=(word>>24)&3; typ=(word>>20)&15; op=(word>>15)&31
    s=(word>>10)&31; m=(word>>5)&31; n=word&31
    if cu==0:
        return typ in (1,4,7,10,13) and op in (16,18,21)   # C-6: COMP, PASS
    if cu==1:
        return typ==5 and (op in (16,17) or op in (8,9) and n==3)  # FCOMP, FR PASS
    if cu==2:
        return typ==11 and op>=24 or typ==12 and op in (8,14,15) or typ==15
    # 'Rs = MR4' only: A-8/10-152 list 'MR4 = Rm {(NF)}' although 5-22
    # excludes "transfer MR4 register instructions"; the listings win.
    return typ==14 and m==3                                # Rs = MR4


def encode(text):
    from .assembler import require
    result=_encode(text)
    if result is not None:
        result.nf_ok=not nf_exempt(result.words[0])
        require(result.nf_ok or not result.words[0]>>30&1,
                f'(NF) is not an option of {text.strip()!r} (PR 3-13, 4-43, 5-22, 6-19)')
        w=result.words[0]
        # A-10: '{X|Y}STAT = Rm', '{X|Y}STAT_L = Rm', '{X|Y}Rs = {X|Y}STAT'.
        require(not ((w>>28)&3==2 and (w>>26)&3==3 and (w>>24)&3==2 and (w>>20)&15==12
                     and (w>>15)&31 in (8,9,14)),'X/YSTAT transfers select one compute block (PR A-10)')
    return result


def _encode(text):
    from .assembler import AssemblyError, Instruction, require, expression
    selected = re.fullmatch(r'(XY|X|Y)\s+(.+)', text, re.I)
    if not selected:
        compact=re.fullmatch(r'(XY|X|Y)(.+)',text,re.I)
        if compact and re.search(r'\b(?:XY|X|Y)?(?:[FLSBQ]?R\d+|(?:[SL]?MR|THR|TR)\d+|CMCTL|PR[01]|STATL?|STAT_L|BFOTMP)\b',compact[2].split('=',1)[-1],re.I):
            selected=compact
    if not selected:
        return None
    block = {'X': 1, 'Y': 2, 'XY': 3}[selected[1].upper()]
    body = selected[2].upper().strip()
    # Operands may repeat the block prefix: 10-70 'XR0 = FIX XFR3:2',
    # 10-93 'XR0 = LOGB XFR1 (S)', 10-181 'XR2 += MASK XR0 BY XR1',
    # A-10/10-195 '{X|Y}Rs = {X|Y}STAT'.
    def operand_prefix(match):
        require(match[1] == selected[1].upper(),
                f'operand {match[0]}... names another compute block than {selected[1].upper()}')
        return ''
    body = re.sub(r'\b(XY|YX|X|Y)(?=(?:[FLSBQ]?R|[SL]?MR|S?TR|THR|PR)\d|(?:STAT|BFOTMP)\b)', operand_prefix, body)
    # 3-7 writes the default option set as '()'.
    body = re.sub(r'\s*\(\s*\)$', '', body)
    options = set()
    while True:
        opt = re.search(r'\s*\((CUT\s+(?:R|[+-]?\d{1,2})|[A-Z, ]+)\)$', body)
        if not opt:
            break
        # Function arguments containing registers must stay in the expression.
        if re.search(r'\bR\d', opt[1]):
            break
        if opt[1].startswith('CUT '):
            values = ['CUT '+opt[1].split()[1]]
        else:
            # The manual uses both (U)(I)(C) and (UIC). Tokenize longest
            # options first so CR, NU, NF, SE and ZE retain their meanings.
            values = []
            for token in re.split(r'[, ]+', opt[1].strip()):
                pieces = re.findall(r'TMAX|CLR|EXT|CR|NU|NF|SE|ZE|ZF|[UICJSTXZ]', token)
                require(''.join(pieces)==token, f'unknown compute option {token}')
                values.extend(pieces)
        for value in values:
            require(value not in options, f'duplicate compute option {value}')
            options.add(value)
        body = body[:opt.start()].strip()
    nf = 'NF' in options
    options.discard('NF')

    def opts(allowed=('',)):
        key = ''.join(sorted(options))
        require(key in allowed, f'unsupported option combination {sorted(options)} for {body}')
        return key

    def reg(token, width=None, kinds=None):
        m = re.fullmatch(r'([FLSBQ]?)R(\d+)(?::(\d+))?', token.strip())
        require(m is not None, f'invalid compute register {token}')
        high = int(m[2]); low = int(m[3]) if m[3] is not None else high
        count = high-low+1
        require(0 <= low <= high <= 31 and count in (1,2,4) and low % count == 0,
                f'compute register range must be aligned and contain 1, 2 or 4 registers: {token}')
        if width is not None:
            require(count == width, f'operand width mismatch: {token}, expected {width} register(s)')
        if kinds is not None:
            require(m[1] in kinds, f'invalid data type for operand {token}')
        return Register(low,count,m[1])

    def emit(cu, typ, op, s=0, m=0, n=0, alternate=False, dests=()):
        require(all(0 <= v < 32 for v in (op,s,m,n)), 'compute encoding field overflow')
        word = 0x20000000 | block << 26 | cu << 24 | typ << 20 | op << 15 | s << 10
        word |= (n << 5 | m) if alternate else (m << 5 | n)
        word |= int(nf) << 30
        writes = frozenset((b,i) for b in (1,2) if block & b
                           for d in dests for i in range(d.index,d.index+d.count))
        return Instruction('compute', [word], resources=frozenset((b,'mul' if cu==3 else 'shift' if cu==2 else 'alu')
                           for b in (1,2) if block & b), writes=writes,
                           two_buses=any(d.count==4 for d in dests) or len(dests)>1,
                           exclusive=cu==2 and (typ in (2,6) or typ==5 and op>=16 or typ==12 and op in (8,9,14)))

    from .clu import encode as clu_encode
    clu = clu_encode(body, options, reg, opts, emit, require, block, nf)
    if clu is not None:
        return clu
    from .compute_extra import encode as extra_encode
    extra = extra_encode(body, options, reg, opts, emit, require)
    if extra is not None:
        return extra

    # C-3 fixed arithmetic, including packed lanes and long words.
    cmp = re.fullmatch(r'(F|L|S|B)?COMP\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)', body)
    assign = re.fullmatch(r'('+R+r')\s*(=|\+=)\s*(.+)', body)
    if cmp:
        kind = cmp[1] or ''
        left = reg(cmp[2],kinds=('',)); right = reg(cmp[3],left.count,('',))
        if kind == 'F':
            opts(); require(left.count in (1,2),'float compare width')
            return emit(1,5,16+int(left.count==2),m=left.index,n=right.index)
        key = opts(('', 'U'))
        typ,tag = fixed_type(Register(0,left.count,kind),require)
        return emit(0,typ+1,16+2*int(key=='U'),m=left.index,n=right.index|tag)
    if not assign:
        # Test-only shifter operation has no destination.
        m = re.fullmatch(r'BITEST\s+('+R+r')\s+BY\s+(.+)', body)
        if m:
            opts(); source=reg(m[1],kinds=('',)); require(source.count in (1,2),'BITEST width')
            n,imm,upper=shift_operand(m[2],6 if source.count==2 else 5,False,reg,expression,require)
            return emit(2,11,24+2*int(source.count==2)+4*imm+upper,m=source.index,n=n,alternate=True)
        raise AssemblyError(f'unsupported compute instruction: {body}')
    dest=reg(assign[1]); update=assign[2]; rhs=assign[3]

    # Floating point arithmetic, conversions and estimates (C-10..12).
    float_source = re.fullmatch(r'(FIX|MANT|LOGB)\s+(FR\d+(?::\d+)?)(?:\s+BY\s+('+R+r'))?',rhs)
    if dest.kind=='F' or float_source:
        require(update=='=', 'floating operations require =')
        require(dest.count in (1,2), 'floating destination must be single or pair')
        source_width = reg(float_source[2]).count if float_source else dest.count
        dual=int(source_width==2)
        key=opts(('', 'T','S'))
        trunc=2*int(key=='T')
        if float_source:
            op= float_source[1]; a=reg(float_source[2],kinds=('F',))
            require(dest.kind=='' and dest.count==1, 'integer result requires one plain R register')
            require(key in (('','S') if op=='LOGB' else ('','T') if op=='FIX' else ('',)), 'invalid float conversion option')
            if float_source[3]:
                require(op=='FIX','only FIX accepts a scale register')
                n=reg(float_source[3],1,('',)).index
                return emit(1,4,28+trunc+dual,dest.index,a.index,n,dests=(dest,))
            return emit(1,5,8+(2*int(key=='S') if op=='LOGB' else trunc)+dual,
                        dest.index,a.index,{'MANT':2,'FIX':7,'LOGB':8}[op],dests=(dest,))
        # Different widths for EXTD and SNGL.
        m=re.fullmatch(r'(EXTD|SNGL)\s+('+R+r')',rhs)
        if m:
            ext=m[1]=='EXTD'; require(dest.count==(2 if ext else 1),'conversion result width')
            a=reg(m[2],1 if ext else 2,('',)); require(key in (('',) if ext else ('','T')),'conversion option')
            return emit(1,5,9 if ext else 8+trunc,dest.index,a.index,9 if ext else 10,dests=(dest,))
        # Two results are encoded in RS and RA.
        m=re.fullmatch(r'('+R+r')\s*\+\s*('+R+r')\s*,\s*('+R+r')\s*=\s*('+R+r')\s*-\s*('+R+r')',rhs)
        if m:
            opts(); a=reg(m[1],dest.count,('',)); b=reg(m[2],dest.count,('',)); other=reg(m[3],dest.count,('F',))
            require(m[1]==m[4] and m[2]==m[5] and other.index!=dest.index,'dual result operands/destinations')
            return emit(1,6+dual,other.index,dest.index,a.index,b.index,dests=(dest,other))
        for pattern,typ,op in [
            (r'('+R+r')\s*\+\s*('+R+r')',4,0), (r'('+R+r')\s*-\s*('+R+r')',4,4),
            (r'\(\s*('+R+r')\s*\+\s*('+R+r')\s*\)\s*/\s*2',4,8),
            (r'\(\s*('+R+r')\s*-\s*('+R+r')\s*\)\s*/\s*2',4,12),
            (r'ABS\s*\(\s*('+R+r')\s*\+\s*('+R+r')\s*\)',4,16),
            (r'ABS\s*\(\s*('+R+r')\s*-\s*('+R+r')\s*\)',4,20),
            (r'CLIP\s+('+R+r')\s+BY\s+('+R+r')',5,0),
            (r'('+R+r')\s+COPYSIGN\s+('+R+r')',5,2),
            (r'MAX\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)',5,12),
            (r'MIN\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)',5,14),
            (r'('+R+r')\s*\*\s*('+R+r')',15,16),
        ]:
            m=re.fullmatch(pattern,rhs)
            if m:
                a=reg(m[1],dest.count,('',)); b=reg(m[2],dest.count,('',))
                require(key in (('','T') if typ in (4,15) else ('',)), 'float option not supported for this operation')
                return emit(3 if typ==15 else 1,typ,op+trunc+dual,dest.index,a.index,b.index,dests=(dest,))
        m=re.fullmatch(r'(FLOAT|SCALB)\s+('+R+r')(?:\s+BY\s+('+R+r'))?',rhs)
        if m:
            is_float=m[1]=='FLOAT'
            a=reg(m[2],1 if is_float else dest.count,('',) if is_float else ('F',))
            require(key in (('','T') if is_float else ('',)), 'conversion option')
            if m[3]:
                n=reg(m[3],1,('',)).index
                return emit(1,4 if is_float else 5,(24+trunc if is_float else 4)+dual,dest.index,a.index,n,dests=(dest,))
            require(is_float,'SCALB requires BY register')
            return emit(1,5,8+trunc+dual,dest.index,a.index,0,dests=(dest,))
        m=re.fullmatch(r'(ABS|PASS|RECIPS|RSQRTS|-)\s*('+R+r')',rhs)
        if m:
            opts(); a=reg(m[2],dest.count,('',))
            return emit(1,5,8+dual,dest.index,a.index,{'ABS':1,'PASS':3,'-':4,'RECIPS':5,'RSQRTS':6}[m[1]],dests=(dest,))
        raise AssemblyError(f'unsupported floating instruction: {body}')

    # Shifts and bit manipulation use the alternate operand field order.
    m=re.fullmatch(r'(LSHIFT|ASHIFT|ROT|BCLR|BSET|BTGL)\s+('+R+r')\s+BY\s+(.+)',rhs)
    if m:
        opts(); require(update=='=','shift assignment must use =')
        a=reg(m[2],dest.count,('',)); op=m[1]
        require(dest.count in (1,2),'shift width')
        if op in ('BCLR','BSET','BTGL'):
            require(dest.kind=='','bit operations require untyped word/pair')
            n,imm,upper=shift_operand(m[3],6 if dest.count==2 else 5,False,reg,expression,require)
            code={'BCLR':0,'BSET':8,'BTGL':16}[op]+2*int(dest.count==2)+4*imm+upper
            return emit(2,11,code,dest.index,a.index,n,True,(dest,))
        if dest.count==1:
            require(dest.kind in ('','S','B'),'invalid scalar shift type')
            typ={'':0,'S':8,'B':9}[dest.kind]
            bits={'':6,'S':5,'B':4}[dest.kind]
            tag=0
        else:
            require(dest.kind in ('','L','S','B'),'invalid paired shift type')
            typ=10 if dest.kind in ('S','B') else 4
            bits=5 if typ==10 else 7
            tag=int(dest.kind in ('L','S'))
        require(op!='ROT' or dest.kind in ('','L'),'packed rotate is not an instruction')
        # A-9 note 2 / 10-171: immediate magnitude follows the output operand
        # (byte 4, short 5, normal 6, long 7 bits), within the C-9/C-10 field.
        semantic={'B':4,'S':5,'':6,'L':7}[dest.kind]
        n,imm,upper=shift_operand(m[3],bits,True,reg,expression,require,semantic)
        if dest.count==2:
            require(a.index%2==0,'pair alignment'); a=Register(a.index|tag,a.count,a.kind)
        if op=='ROT': typ+=1
        code=(8 if op=='ASHIFT' else 0)+4*imm+upper
        return emit(2,typ,code,dest.index,a.index,n,True,(dest,))
    m=re.fullmatch(r'(LD0|LD1|EXP)\s+('+R+r')',rhs)
    if m:
        opts(); require(dest.kind=='' and dest.count==1 and update=='=','analysis result must be one word')
        a=reg(m[2],kinds=('',)); require(a.count in (1,2),'analysis source width')
        return emit(2,12,{'LD0':0,'LD1':1,'EXP':4}[m[1]]+2*int(a.count==2),dest.index,a.index,dests=(dest,))

    # Scalar/dual integer products (C-16), independent of the ALU.
    m=re.fullmatch(r'('+R+r')\s*\*\s*('+R+r')',rhs)
    if m:
        require(update=='=' and dest.kind=='','integer product requires plain R result')
        a=reg(m[1],kinds=('',)); b=reg(m[2],a.count,('',)); require(a.count in (1,2),'multiply operand width')
        require(dest.count in (a.count,2*a.count),'multiply result width')
        require(options <= {'I','U','NU','S','T'},'invalid multiply options')
        require(not {'U','NU'} <= options and not {'I','T'} <= options,'conflicting multiply options')
        wide=dest.count==2*a.count
        require(not wide or not ({'S','T'} & options),'wide multiply has implicit saturation bit and no S/T option')
        require(a.count==1 or 'NU' not in options,'NU is scalar multiply only')
        typ=(0 if a.count==1 else 2)+int(wide)
        code=16*int('U'in options)+8*int('I'in options)+4*int(wide or 'S'in options)+2*int('T'in options)+int('NU'in options)
        return emit(3,typ,code,dest.index,a.index,b.index,dests=(dest,))

    require(update=='=', 'unsupported accumulate instruction')
    typ,tag=fixed_type(dest,require)
    # Compound add/sub has a dedicated second destination, not two operations.
    m=re.fullmatch(r'('+R+r')\s*\+\s*('+R+r')\s*,\s*('+R+r')\s*=\s*('+R+r')\s*-\s*('+R+r')',rhs)
    if m:
        # A-3 prints the second result as plain Ra/Rad; the typed spelling
        # (SRa, LRad) is also accepted.
        opts(); a=reg(m[1],dest.count,('',)); b=reg(m[2],dest.count,('',)); other=reg(m[3],dest.count,(dest.kind,''))
        require(m[1]==m[4] and m[2]==m[5] and other.index!=dest.index,'dual result operands/destinations')
        return emit(0,typ+2,other.index,dest.index,a.index,b.index|tag,dests=(dest,other))
    patterns=[
        (r'('+R+r')\s*\+\s*('+R+r')',0,0,'sat'),
        (r'('+R+r')\s*-\s*('+R+r')',0,4,'sat'),
        (r'ABS\s*\(\s*('+R+r')\s*\+\s*('+R+r')\s*\)',0,8,'extend'),
        (r'ABS\s*\(\s*('+R+r')\s*-\s*('+R+r')\s*\)',0,12,'uextend'),
        (r'\(\s*('+R+r')\s*\+\s*('+R+r')\s*\)\s*/\s*2',0,16,'half'),
        (r'\(\s*('+R+r')\s*-\s*('+R+r')\s*\)\s*/\s*2',0,20,'half'),
        (r'MAX\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)',0,24,'minmax'),
        (r'MIN\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)',0,28,'minmax'),
        (r'('+R+r')\s*\+\s*('+R+r')\s*\+\s*CI',1,0,'word_sat'),
        (r'('+R+r')\s*-\s*('+R+r')\s*\+\s*CI\s*-\s*1',1,4,'word_sat'),
        (r'CLIP\s+('+R+r')\s+BY\s+('+R+r')',1,20,'none'),
        (r'('+R+r')\s+AND\s+NOT\s+('+R+r')',1,31,'word'),
        (r'('+R+r')\s+AND\s+('+R+r')',1,24,'word'),
        (r'('+R+r')\s+OR\s+('+R+r')',1,25,'word'),
        (r'('+R+r')\s+XOR\s+('+R+r')',1,26,'word'),
    ]
    option_map={'sat':{'':0,'S':1,'SU':3},'word_sat':{'':0,'S':1,'SU':3},
                'extend':{'':0,'X':1},'uextend':{'':0,'X':1,'U':2,'UX':3},
                'half':{'':0,'T':1,'U':2,'TU':3},'minmax':{'':0,'Z':1,'U':2,'UZ':3},
                'none':{'':0},'word':{'':0}}
    for pattern,delta,op,allowed in patterns:
        m=re.fullmatch(pattern,rhs)
        if m:
            key=opts(tuple(option_map[allowed])); a=reg(m[1],dest.count,('',)); b=reg(m[2],dest.count,('',))
            if allowed.startswith('word'):
                require(dest.kind=='' and dest.count==1 or dest.kind=='L' and dest.count==2,'operation permits only word or long')
            return emit(0,typ+delta,op+option_map[allowed][key],dest.index,a.index,b.index|tag,dests=(dest,))
    m=re.fullmatch(r'(INC|DEC|PASS|ABS|NOT|-)\s*('+R+r')',rhs)
    if m:
        name=m[1]; a=reg(m[2],dest.count,('',))
        key=opts(('', 'S','SU') if name in ('INC','DEC') else ('',))
        if name in ('PASS','NOT'):
            require(dest.kind=='' and dest.count==1 or dest.kind=='L' and dest.count==2,'operation permits word or long only')
        code={'INC':8,'DEC':12,'PASS':21,'ABS':22,'-':23,'NOT':27}[name]+{'':0,'S':1,'SU':3}[key]
        return emit(0,typ+1,code,dest.index,a.index,tag,dests=(dest,))
    m=re.fullmatch(r'('+R+r')\s*\+\s*CI(\s*-\s*1)?',rhs)
    if m:
        opts(); require(dest.kind=='' and dest.count==1 or dest.kind=='L' and dest.count==2,'carry operation width')
        a=reg(m[1],dest.count,('',))
        return emit(0,typ+1,29 if m[2] else 28,dest.index,a.index,tag,dests=(dest,))
    raise AssemblyError(f'unsupported compute instruction: {body}')


def fixed_type(dest, require):
    require(dest.count in (1,2),'fixed ALU supports single or paired register results')
    if dest.count==1:
        require(dest.kind in ('','S','B'),'invalid scalar fixed ALU type')
        return {'':0,'S':6,'B':9}[dest.kind],0
    require(dest.kind in ('','L','S','B'),'invalid paired fixed ALU type')
    return (12 if dest.kind in ('S','B') else 3),int(dest.kind in ('S','L'))


def shift_operand(text,bits,signed,reg,expression,require,range_bits=None):
    if re.fullmatch(R,text):
        return reg(text,1,('',)).index,0,0
    value=expression(text)
    width=range_bits or bits
    low=-(1<<(width-1)) if signed else 0
    high=(1<<(width-1))-1 if signed else (1<<width)-1
    require(low<=value<=high,f'shift/bit immediate must be {low}..{high}')
    value &= (1<<bits)-1
    return value&31,1,value>>5
