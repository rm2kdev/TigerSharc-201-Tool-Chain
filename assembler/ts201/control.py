"""Program sequencer encodings, PR Rev 1.1 C-38..47 (Tables C-21..28)."""
import re

CONDITIONS = {'JEQ':0,'JLT':1,'JLE':2,'KEQ':4,'KLT':5,'KLE':6,
              'TRUE':8,'ISF0':9,'ISF1':10,'BM':11,'LC0E':12,'LC1E':13,
              **{f'FLAG{i}_IN':60+i for i in range(4)}}
# No block prefix = both blocks (C-44 <XY>=11; 3-15 Table 3-3; 10-234).
for prefix,block in (('X',1),('Y',2),('XY',3),('',3)):
    for name,value in {'AEQ':0,'ALT':1,'ALE':2,'MEQ':3,'MLT':4,'MLE':5,'SEQ':6,'SLT':7,'SF0':9,'SF1':10}.items():
        CONDITIONS[prefix+name] = block*16+value


def condition(name):
    from .assembler import require
    name=name.upper()
    negate=name.startswith('N')
    if negate: name=name[1:]
    require(name in CONDITIONS, f'unknown condition {name}')
    return CONDITIONS[name],int(negate)


def encode(text,symbols=None,pc=0,layout=False):
    from .assembler import Instruction, require, expression, has_symbol, word_value
    text=text.strip()
    flag=re.fullmatch(r'(ISF[01]|(?:XY|X|Y)?SF[01])\s*(=|\+=)\s*(?:(AND|OR|XOR)\s+)?(\w+)',text,re.I)
    if flag:
        dest,assignment,logic,cond=flag.groups(); dest=dest.upper()
        require((assignment=='=')==(logic is None),'static flag logic requires +=; plain assignment requires =')
        code,neg=condition(cond)
        require(dest.startswith('I') or 16<=code<60,
                'compute static flags cannot be assigned non-compute conditions (PR 1-49)')
        prefix=dest[:-3]; select=int(dest[-1])+(0 if dest.startswith('I') else {'X':2,'Y':4,'XY':6,'':6}[prefix])
        operation=1 if logic is None else {'AND':4,'OR':5,'XOR':6}[logic.upper()]
        return Instruction('flag',[0x33000000|neg<<22|code<<16|operation<<10|select<<7])
    simple={'NOP':0,'IDLE':0x40000,'BTBINV':0x80000,'BTBEN':0x80002,
            'BTBDIS':0x80004,'BTBLOCK':0x80008,'BTBELOCK':0x80010,'EMUTRAP':0x200000}
    if text.upper() in simple:
        # NOP may fill any slot (PR C-42 'jump <label>; nop; nop; nop;;',
        # 8-95 'JUMP _loop (NP); nop;;'); the others stay in slot one.
        return Instruction('nop' if text.upper()=='NOP' else 'misc',[0x33C00000|simple[text.upper()]])
    trap=re.fullmatch(r'TRAP\s*\((.+)\)',text,re.I)
    if trap:
        value=expression(trap[1]); require(0<=value<=31,'TRAP command must be 0..31')
        return Instruction('misc',[0x33D00000|value])
    reduce=re.fullmatch(r'(?:IF\s+(\w+)\s*,\s*)?RDS',text,re.I)
    if reduce:
        code,neg=condition(reduce[1] or 'TRUE')
        return Instruction('reduce',[0x33000040|neg<<22|code<<16])
    m=re.fullmatch(r'(?:IF\s+(\w+)\s*,\s*)?(JUMP|CALL|CJMP_CALL|CJMP|RTI|RETI)\b(?!\s*=)(.*)',text,re.I)
    if not m: return None
    code,neg=condition(m[1] or 'TRUE')
    name=m[2].upper(); tail=m[3].strip(); options=[]
    while (match:=re.search(r'\s*\((NP|BP|ABS|NF|S)\)$',tail,re.I)):
        options.insert(0,match[1].upper()); tail=tail[:match.start()].strip()
    require(len(set(options))==len(options),'duplicate branch option')
    # PR C-39/C-41: BP=1 by default, cleared by (NP). (BP) is a non-manual
    # spelling of the default, kept for existing sources.
    require(not {'NP','BP'}<=set(options),'(NP) and (BP) are contradictory')
    predicted='NP' not in options; absolute='ABS' in options
    base=int(predicted)<<30 | neg<<22 | code<<16 | int('NF' in options)<<15
    if name in ('JUMP','CALL'):
        require(bool(tail),'missing branch target'); has_symbol(tail)
        target=0 if layout else expression(tail,symbols)
        value=word_value(target if absolute or layout else target-pc)
        first=0x30000000|base|int(not absolute)<<24|int(name=='CALL')<<23|value&0x7fff
        if 'S' in options:
            # Toolchain extension: force the one-slot 15-bit PC-relative form
            # (PR 1-35 "Conditional Jump/Call, 15-bit immediate"; 8-17). The
            # compiler requests it so packet packing never depends on layout.
            require(not absolute,'(S) short branch must be PC-relative')
            require(layout or -0x4000<=target-pc<0x4000,'(S) short branch target out of 15-bit range')
            return Instruction('branch',[first],target=tail,absolute=absolute,predicted=predicted)
        return Instruction('branch',[first,0x33800000|value>>15],target=tail,absolute=absolute,predicted=predicted)
    require('S' not in options,'(S) applies to JUMP/CALL with a target')
    require(not tail,'indirect branch does not take a target operand')
    # 10-236: "If the prediction is true ..., the call must be absolute".
    # 8-16 and 10-242 use a predicted PC-relative CJMP ('If MEQ, CJMP; ...').
    require(name!='CJMP_CALL' or not predicted or absolute,
            'predicted CJMP_CALL requires ABS (PR 10-236)')
    mode=1 if name in ('RTI','RETI') else 2 if absolute else 3
    return Instruction('indirect',[0x33000000|base|mode<<13|int(name=='RTI')<<6|int(name=='CJMP_CALL')<<5],predicted=predicted)
