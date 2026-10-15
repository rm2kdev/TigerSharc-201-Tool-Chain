"""CLU encodings, PR C-12..14 and C-25. Independently derived opcode fields."""
import re
from .compute import R

T = r'S?TR\d{1,2}(?::\d{1,2})?'
H = r'THR\d{1,2}(?::\d{1,2})?'


def encode(body,options,reg,opts,emit,require,block,nf):
    from .assembler import Instruction
    def trellis(text,width=None,threshold=False):
        pattern=r'(S?)TR(\d+)(?::(\d+))?' if not threshold else r'()THR(\d+)(?::(\d+))?'
        m=re.fullmatch(pattern,text)
        require(m is not None,'invalid trellis register')
        high=int(m[2]); low=int(m[3]) if m[3] else high; count=high-low+1
        require(0<=low<=high<(16 if threshold else 32) and count in (1,2,4) and low%count==0,'unaligned or invalid trellis range')
        require(width is None or count==width,'trellis width mismatch')
        return low,count,bool(m[1])

    def special_writes(index,count):
        return frozenset((b,i) for b in (1,2) if block&b for i in range(index,index+count))

    def clu(fields,dests=(),trellis_dest=None):
        # C-2 explicitly reverses X/Y selection for CLU instruction class:
        # 1101=Y, 1110=X, 1111=XY. Keep this independent of Cpu.CluRead.
        selection={1:2,2:1,3:3}[block]
        writes=frozenset((b,i) for b in (1,2) if block&b for d in dests for i in range(d.index,d.index+d.count))
        if trellis_dest is not None:
            index,count=trellis_dest
            writes |= special_writes(128+index,count)
        return Instruction('compute',[0x30000000|selection<<26|int(nf)<<30|fields],
                           resources=frozenset((b,'clu') for b in (1,2) if block&b),writes=writes,
                           two_buses=any(d.count==4 for d in dests))

    m=re.fullmatch(r'(?:('+R+r')\s*=\s*('+T+r')\s*,\s*)?TR(31:16|15:0)\s*=\s*XCORRS\s*\(\s*('+R+r')\s*,\s*('+H+r')\s*\)',body)
    if m:
        cuts=[o for o in options if o.startswith('CUT ')]
        require(len(cuts)<=1 and options-set(cuts)<={'CLR','EXT'},'XCORRS accepts one CUT -22..22 or CUT R option')
        cut=cuts[0].split()[1] if cuts else '0'; by_register=cut=='R'; value=0 if by_register else int(cut)
        require(-22<=value<=22,'XCORRS cut must be -22..22 (PR 10-104)')
        value &= 63
        a=reg(m[4],4,('',)); h,_,_=trellis(m[5],4,True)
        lower=m[3]=='15:0'
        fields=(12+int(bool(m[1]))+2*int('EXT'in options))<<20|int(lower)<<19
        dests=()
        if m[1]:
            d=reg(m[1],4,('',)); t,_,short=trellis(m[2],4)
            # C-14 footnote 6: TRsq[4]=THL. The result transfer reads
            # the opposite half from the half being accumulated.
            require(not short and t//16==int(lower),'XCORRS transfer must read the opposite trellis half (PR C-14)')
            fields|=((t>>2)&3)<<17|(d.index>>2)<<12
            dests=(d,)
        fields|=(value>>4)<<15|int('CLR'in options)<<11|int(by_register)<<10|(a.index>>2)<<7|((value>>2)&3)<<5|h|(value&3)
        return clu(fields,dests,(0 if lower else 16,16))
    m=re.fullmatch(r'('+R+r')\s*=\s*('+T+'|'+H+r'|CMCTL)',body)
    if m:
        opts(); d=reg(m[1],kinds=('',))
        if m[2]=='CMCTL':
            require(d.count==1,'CMCTL is scalar'); index,width=31,1; threshold=True
        else:
            threshold=m[2].startswith('THR'); index,width,short=trellis(m[2],d.count,threshold)
            require(not short,'trellis transfer is untyped')
        size={1:0,2:2,4:3}[width]
        return clu(int(threshold)<<15|d.index<<10|size<<5|index,(d,))
    m=re.fullmatch(r'('+T+'|'+H+r'|CMCTL)\s*=\s*('+R+r')',body)
    if m:
        key=opts(('', 'I')); a=reg(m[2],kinds=('',))
        if m[1]=='CMCTL':
            require(a.count==1 and not key,'CMCTL load shape'); index=31; op=2
        else:
            threshold=m[1].startswith('THR'); index,width,short=trellis(m[1],a.count,threshold)
            require(not short and (threshold or not key),'interleave is THR-only')
            op=2+int(key=='I') if threshold else 0
        # C-25 note 1: standard compute format, Rm in bits 9:5. The prose
        # "Bits[9:7] = Rm" conflicts with 10-198 'XYTR1:0 = R3:2'; the
        # register only needs its natural pair/quad alignment.
        fields=((index&1)<<19)|((index>>1)<<15)|(op<<10)|(a.index<<5)|({1:0,2:2,4:3}[a.count]<<3)
        return Instruction('compute',[0x20000000|block<<26|2<<24|15<<20|int(nf)<<30|fields],
                           resources=frozenset((b,'shift') for b in (1,2) if block&b),
                           writes=special_writes(176 if m[1]=='CMCTL' else (160 if threshold else 128)+index,a.count))
    m=re.fullmatch(r'('+R+r')\s*=\s*TMAX\s*\(\s*('+T+r')\s*,\s*('+T+r')\s*\)',body)
    if m:
        opts(); d=reg(m[1],1,('','S')); a,_,short=trellis(m[2],1); b,_,short2=trellis(m[3],1)
        require(not short and not short2,'TMAX operand types are selected by destination')
        return clu(3<<20|int(d.kind=='')<<15|d.index<<10|b<<5|a,(d,))
    # PR 10-97/10-99 examples: 'TMAX(TR3:2 + R3:2, TR5:4 + R1:0)' - Rmq_h and
    # Rmq_l are the upper and lower pairs of one quad. The older 'R3:0_H'/
    # 'R3:0_L' spelling of the same quad is still accepted.
    m=re.fullmatch(r'('+T+r')\s*=\s*(MAX|TMAX)\s*\(\s*('+T+r')\s*(\+|-)\s*('+R+r')(_H)?\s*,\s*('+T+r')\s*(\+|-)\s*('+R+r')(_L)?\s*\)',body)
    if m:
        opts(); d,_,short=trellis(m[1],2); a,_,sa=trellis(m[3],2); b,_,sb=trellis(m[7],2)
        require(bool(m[6])==bool(m[10]),'use Rmq_h/Rmq_l pairs or both _H/_L suffixes')
        if m[6]:
            require(m[5]==m[9],'trellis MAX _H/_L must name the same quad')
            r=reg(m[5],4,('',))
        else:
            high=reg(m[5],2,('',)); low=reg(m[9],2,('',))
            require(low.index%4==0 and high.index==low.index+2,
                    'Rmq_h/Rmq_l must be the upper and lower halves of one quad (PR 10-97)')
            r=low
        require(not sa and not sb and m[4]==m[8],'trellis MAX source shape')
        fields=2<<20|int(m[2]=='TMAX')<<19|(d>>1)<<15|(b>>1)<<11|int(m[4]=='-')<<10|(r.index>>2)<<7|a|int(not short)
        return clu(fields,trellis_dest=(d,2))
    m=re.fullmatch(r'('+R+r')\s*=\s*('+T+r')\s*,\s*('+T+r')\s*=\s*ACS\s*\(\s*('+T+r')\s*,\s*('+T+r')\s*,\s*('+R+r')\s*\)',body)
    if m:
        key=opts(('', 'TMAX')); out=reg(m[1],4,('',)); old,_,st=trellis(m[2],4)
        d,_,short=trellis(m[3],4); a,_,sa=trellis(m[4],2); b,_,sb=trellis(m[5],2); r=reg(m[6],1,('',))
        require(not st and not sa and not sb,'ACS transfer types')
        return clu(1<<24|(out.index>>2)<<21|(old>>4)<<20|(d>>2)<<17|((old>>2)&3)<<15|(b>>1)<<11|int(key=='TMAX')<<10|r.index<<5|a|int(not short),(out,),(d,4))
    m=re.fullmatch(r'('+T+r')\s*=\s*ACS\s*\(\s*('+T+r')\s*,\s*('+T+r')\s*,\s*('+R+r')\s*\)',body)
    if m:
        key=opts(('', 'TMAX')); d,_,short=trellis(m[1],4); a,_,sa=trellis(m[2],2); b,_,sb=trellis(m[3],2); r=reg(m[4],1,('',))
        require(not sa and not sb,'ACS source types are selected by destination')
        return clu(4<<20|(d>>2)<<16|(b>>1)<<11|int(key=='TMAX')<<10|r.index<<5|a|int(not short),trellis_dest=(d,4))
    m=re.fullmatch(r'('+T+r')\s*\+=\s*DESPREAD\s*\(\s*('+R+r')\s*,\s*('+H+r')\s*\)',body)
    if m:
        opts(); d,_,short=trellis(m[1],1); a=reg(m[2],4,('',)); h,_,_=trellis(m[3],2,True)
        require(not short,'DESPREAD requires full trellis word')
        return clu(8<<20|(d&1)<<19|(d>>1)<<15|(a.index>>2)<<7|h,trellis_dest=(d,1))
    m=re.fullmatch(r'('+R+r')\s*=\s*('+T+r')\s*,\s*('+T+r')\s*=\s*DESPREAD\s*\(\s*('+R+r')\s*,\s*('+H+r')\s*\)',body)
    if m:
        opts(); d=reg(m[1],kinds=('',)); t,_,short=trellis(m[2],d.count); a=reg(m[4],4,('',)); h,_,_=trellis(m[5],2,True)
        require(d.count in (1,2) and not short and m[2]==m[3],'DESPREAD transfer shape')
        return clu((9 if d.count==1 else 11)<<20|(t&1)<<19|(t>>1)<<15|d.index<<10|(a.index>>2)<<7|h,(d,),(t,d.count))
    return None
