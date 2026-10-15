"""Named compute/IALU/sequencer register transfers. PR C-29..34, Appendix B.

The list is explicit to reject reserved register encodings. Dynamic memory
alignment remains the program's obligation; literal width/offsets are checked.
"""
import re
from .registers import REGISTERS, check

SPECIAL = {name:(s.group,s.index,s.width) for name,s in REGISTERS.items()}
UR = r'(?:(?:XY|YX|X|Y)?R(?:[12]?\d|3[01])(?::(?:[12]?\d|3[01]))?|[JK][0-9]+(?::[0-9]+)?|'+'|'.join(SPECIAL)+')'


def float_literal(text):
    """IEEE-754 binary32 bits of a signed decimal floating literal, else None."""
    import struct
    m=re.fullmatch(r'\s*([+-]?)\s*(\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)\s*',text)
    if not m: return None
    from .assembler import require
    value=float(m[1]+m[2])
    try:
        return struct.unpack('<I',struct.pack('<f',value))[0]
    except OverflowError:
        require(False,f'floating literal {text.strip()} exceeds single precision')


def ureg(text):
    from .assembler import require, reg
    name=text.upper().strip()
    if name in SPECIAL:
        return SPECIAL[name]
    if re.fullmatch(r'[JK]\d+',name):
        bank,index=reg(name,ureg=True)
        return 12+bank,index,1
    pair=re.fullmatch(r'([JK])(\d+):(\d+)',name)
    if pair:
        high,low=int(pair[2]),int(pair[3]); width=high-low+1
        require(0<=low<=high<=31 and width in (2,4) and low%width==0,'IALU transfer range must be aligned')
        return 12+int(pair[1]=='K'),low,width
    m=re.fullmatch(r'(XY|YX|X|Y|)R(\d+)(?::(\d+))?',name)
    require(m is not None,f'unsupported universal register {text}')
    high=int(m[2]); low=int(m[3]) if m[3] else high; count=high-low+1
    require(0<=low<=high<=31 and count in (1,2,4) and low%count==0,'transfer register range must be aligned')
    # XY defaults to broadcast; memory's explicit L/Q size selects merge.
    # No prefix = XY (PR 2-7, 7-10).
    return {'X':0,'Y':2,'XY':8,'YX':6,'':8}[m[1]],low,count


def encode(text,symbols=None,pc=0,layout=False):
    from .assembler import require
    nf=bool(re.search(r'\s*\(NF\)$',text,re.I))
    if nf: text=re.sub(r'\s*\(NF\)$','',text,flags=re.I)
    result=encode_inner(text,symbols,pc,layout)
    if result is not None:
        # Data transfers generate no flags; Listings 7-7..7-11/A-13..A-17
        # have no (NF) option (7-11: NF is for arithmetic/logic/function).
        require(not nf,'(NF) is not an option of data transfers (PR 7-11, A-13..A-17)')
        result.nf_ok=False
    return result


def encode_inner(text,symbols=None,pc=0,layout=False):
    from .assembler import Instruction, require, expression, has_symbol, word_value, signed_word, reg, operand
    move=re.fullmatch(r'('+UR+r')\s*=\s*('+UR+r')',text,re.I)
    if move:
        check(move[1],'move',True); check(move[2],'move',False)
        source=move[2]
        block=re.match(r'(X|Y)R\d',move[1].strip(),re.I)
        if block and re.fullmatch(r'R\d+(?::\d+)?',source.strip(),re.I):
            # 1-45 'XR3:0 = R7:4': the destination prefix names the one
            # register file of the transfer.
            source=block[1]+source.strip()
        dg,di,width=ureg(move[1]); sg,si,sw=ureg(source)
        if re.fullmatch(r'CACMD(?:\d+|B)',move[1].strip(),re.I) and sw==2:
            # A-17: 'CACMDx = Jmd; /* long word transfer */' also loads CCAIRx.
            width=2
        debug=lambda g:g in (10,27,61,62,63)
        seq=lambda g:g==26 or 48<=g<=59
        require(not (debug(sg) and (seq(dg) or debug(dg)) or debug(dg) and seq(sg)),
                'debug/sequencer and debug/debug register moves are forbidden (PR 1-48)')
        if dg in (30,31):
            require(sg in (12,13) and si+sw<=31,'cache command/address move source must be J0..30/K0..30 (PR 9-20)')
        if dg in (6,8) and sg>=12 and width*2==sw:
            dg=4 if dg==8 else 6; width=sw
        elif sg in (6,8) and dg>=12 and sw*2==width:
            sg=4 if sg==8 else 6; sw=width
        elif sg==8 and dg==8:
            pass  # SIMD copy in each block, PR 7-42.
        else:
            require(sg not in (6,8) and dg not in (6,8),
                    'XY/YX move must be SIMD XY-to-XY or merged with non-compute registers (PR 1-44)')
        require(width==sw,'move widths differ')
        unit=int(dg in (13,15) or sg in (13,15))
        w=0x08000000|unit<<28|(sg&31)<<21|si<<16|int(width>1)<<14|dg<<8|((sg>>5)&1)<<7|di<<1|int(width==4)
        return Instruction('move',[w],unit)
    load=re.fullmatch(r'('+UR+r')\s*=\s*(?:(CB|BR|DAB|SDAB|XDAB|XSDAB|YDAB|YSDAB)\s+)?([LQ])?\s*\[\s*([JK]\d+)\s*(\+=|\+)\s*(.+?)\s*\]',text,re.I)
    store=re.fullmatch(r'(?:(CB|BR)\s+)?([LQ])?\s*\[\s*([JK]\d+)\s*(\+=|\+)\s*(.+?)\s*\]\s*=\s*('+UR+r')',text,re.I)
    if load or store:
        data,mode,size,base,modify,offset=load.groups() if load else (store[6],store[1],store[2],store[3],store[4],store[5])
        check(data,'memory',load is not None)
        group,index,register_width=ureg(data); unit,base_index=reg(base)
        mode=(mode or '').upper()
        width={'L':2,'Q':4}[size.upper()] if size else register_width
        if group in (6,8):
            if size and width==2*register_width:
                group=4 if group==8 else 6
            else:
                require(group==8 and width==register_width and load is not None,
                        'XY broadcast is load-only; XY/YX merge requires L with a scalar or Q with a pair')
        else:
            require(width==register_width,'explicit memory size differs from register width')
        require(base_index!=31 or modify!='+=','cannot post-modify J31/K31')
        require(not (load and modify=='+=' and group==12+unit and index<=base_index<index+width),'post-modify load writes the same register twice')
        extension=[]
        if re.fullmatch(r'[JK]\d+',offset,re.I):
            bits=operand(offset,unit)
        else:
            symbolic=has_symbol(offset)
            value=0 if layout and symbolic else word_value(expression(offset,symbols))
            require(width==1 or value%width==0,'wide transfer offset must be aligned in DSP words')
            # A literal direct address must name internal RAM; a symbolic one
            # (PR 7-15, 8-84 `[J31 + external_address]`) lies wherever the linker
            # placed its symbol, including configured external memory windows.
            if base_index==31 and not symbolic:
                from .assembler import BANKS
                require(any(lo<=value and value+width<=hi for lo,hi in BANKS),'absolute memory address is outside internal RAM')
            bits=0x2000|(value&255)
            if symbolic or not -128<=signed_word(value)<=127:
                extension=[0x32000000|unit<<30|value>>8]
        if mode:
            require(group in (0,2,4,6,8) and base_index<4,'alternate addressing requires compute data and J0..3/K0..3')
            require(modify=='+=','alternate addressing requires post-modify (PR 7-16)')
            if mode not in ('CB','BR'):
                require(load and width==4 and register_width==4,'DAB requires a normal or broadcast quad load')
                require(mode in ('DAB','SDAB') or group==8,'offset DAB requires an XY broadcast destination')
            group+=1
            base_index|={'CB':0,'BR':1,'DAB':2,'SDAB':3,'XDAB':4,'XSDAB':5,'YDAB':6,'YSDAB':7}[mode]<<2
        w=unit<<28|int(store is not None)<<26|group<<21|index<<16|int(modify=='+=')<<15|int(width>1)<<14|base_index<<8|bits|int(width==4)
        return Instruction('memory',[w]+extension,unit)
    const=re.fullmatch(r'('+UR+r')\s*=\s*(.+)',text,re.I)
    if const:
        check(const[1],'constant',True)
        group,index,width=ureg(const[1]); require(width==1 and group!=6,'constant load requires a scalar normal or XY broadcast register')
        real=float_literal(const[2])
        if real is not None:
            # 10-86/10-88: 'YR0 = 10.0' loads the IEEE single-precision bits.
            symbolic=False; value=real
        else:
            symbolic=has_symbol(const[2]); value=0 if layout and symbolic else word_value(expression(const[2],symbols))
        unit=int(group in (13,15)); first=0x08008000|unit<<28|group<<21|index<<16|value&0x7fff
        words=[first]
        if symbolic or not -16384<=signed_word(value)<=16383:
            # PR C-37 Fig C-10: extension = value bits 31:8, as for imm8
            # forms; imm15 bits 14:8 duplicate extension bits 6:0.
            words.append(0x32000000|unit<<30|value>>8)
        return Instruction('constant',words,unit)
    return None
