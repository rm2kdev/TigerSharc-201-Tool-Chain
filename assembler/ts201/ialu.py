"""Additional binary IALU forms, PR C-26..29 and C-37."""
import re


def encode(text,symbols=None,pc=0,layout=False):
    from .assembler import Instruction, require, reg, REG, expression, has_symbol, word_value
    compare=re.fullmatch(r'COMP\s*\(\s*'+REG+r'\s*,\s*(.+?)\s*\)\s*(\(U\))?',text,re.I)
    if compare:
        unit,left=reg(compare[1]); right=compare[2]; extension=[]
        if re.fullmatch(REG,right,re.I):
            other,index=reg(right); require(other==unit,'mixed J/K operands in one IALU operation'); bits=index<<1
        else:
            symbolic=has_symbol(right); value=0 if layout and symbolic else word_value(expression(right,symbols))
            signed=value if value<0x80000000 else value-0x100000000
            bits=0x2000|(value&255)
            if symbolic or not -128<=signed<=127: extension=[0x32000000|unit<<30|value>>8]
        # COMP takes no (NF) and always updates flags (PR 7-11 IALU NF note).
        return Instruction('alu',[0x0c000000|unit<<28|(12+bool(compare[3]))<<21|left<<8|bits]+extension,unit,nf_ok=False)
    body=text.strip(); options=set()
    while (m:=re.search(r'\s*\((CJMP|CB|BR|NF)\)$',body,re.I)):
        require(m[1].upper() not in options,'duplicate IALU option')
        options.add(m[1].upper()); body=body[:m.start()].strip()
    assign=re.fullmatch(r'([JK](?:[BL][0-3]|[0-9]|[12][0-9]|3[01]))\s*=\s*(.+)',body,re.I)
    if not assign: return None
    dest_text,rhs=assign.groups(); circular=bool(re.fullmatch(r'[JK][BL][0-3]',dest_text,re.I))
    if circular:
        unit=int(dest_text[0].upper()=='K'); dest=int(dest_text[-1])+4*int(dest_text[1].upper()=='L')
    else: unit,dest=reg(dest_text,dest=True)
    unary=re.fullmatch(r'(NOT|ABS|ASHIFTR|LSHIFTR|ROTR|ROTL)\s+'+REG,rhs,re.I)
    if unary:
        require(not circular,'base/length destination requires addition or subtraction')
        require(options<={'NF'},'unary IALU accepts only NF')
        other,index=reg(unary[2]); require(other==unit,'mixed J/K operands in one IALU operation')
        opcode={'NOT':24,'ABS':25,'ASHIFTR':26,'LSHIFTR':27,'ROTR':28,'ROTL':29}[unary[1].upper()]
        # C-13/C-14 name this source Jm/Km (bits 12:8).
        return Instruction('alu',[0x0c000000|unit<<28|int('NF'in options)<<30|opcode<<21|dest<<16|index<<8],unit)
    patterns=[
        (r'(MIN|MAX)\s*\(\s*'+REG+r'\s*,\s*(.+?)\s*\)', 'minmax'),
        (r'\(\s*'+REG+r'\s*(\+|-)\s*(.+?)\s*\)\s*/\s*2','half'),
        (REG+r'\s*(\+|-)\s*(.+?)\s*\+\s*([JK])C(\s*-\s*1)?','carry'),
        (REG+r'\s*(AND NOT|AND|OR|XOR|\+|-)\s*(.+)','binary'),
    ]
    for pattern,kind in patterns:
        m=re.fullmatch(pattern,rhs,re.I)
        if not m: continue
        if kind=='minmax':
            op=14 if m[1].upper()=='MIN' else 15; left,right=m[2],m[3]
        else:
            left,right=m[1],m[3]; action=m[2].upper()
            if kind=='half': op=10+int(action=='-')
            elif kind=='carry':
                require(m[4].upper()==('K' if unit else 'J'),'carry must use the same IALU')
                require(bool(m[5])==(action=='-'),'borrow form requires + JC/KC - 1')
                op=16+int(action=='-')
            else: op={'+':0,'-':1,'OR':18,'AND':19,'XOR':20,'AND NOT':21}[action]
        lu,li=reg(left); require(unit==lu,'mixed J/K operands in one IALU operation')
        modes=options-{'NF'}
        require(len(modes)<=1,'conflicting IALU modes')
        if modes:
            require(kind=='binary' and op in (0,1),'address mode requires addition/subtraction')
            if 'CB' in modes: require(li<4,'CB source index must be J0..J3 or K0..K3')
            op += {'CJMP':2,'CB':4,'BR':6}[next(iter(modes))]
        if circular:
            require(kind=='binary' and op in (0,1) and not modes,'base/length destination requires plain addition/subtraction')
            op+=8
        extension=[]
        if re.fullmatch(REG,right,re.I):
            ru,ri=reg(right); require(ru==unit,'mixed J/K operands in one IALU operation')
            bits=ri<<1
        else:
            symbolic=has_symbol(right)
            value=0 if layout and symbolic else word_value(expression(right,symbols))
            signed=value if value<0x80000000 else value-0x100000000
            bits=0x2000|(value&255)
            if symbolic or not -128<=signed<=127:
                extension=[0x32000000|unit<<30|value>>8]
        first=0x0c000000|unit<<28|int('NF'in options)<<30|op<<21|dest<<16|li<<8|bits
        return Instruction('alu',[first]+extension,unit)
    return None
