"""Data conversion, accumulator and field operations from PR Tables C-4/7/8/9/11.

This module receives checked register/options/field constructors. Its independent
field-vector tests do not import these dispatch tables.
"""
import re
from .compute import R


def encode(body,options,reg,opts,emit,require):
    # Multiplier accumulator pair a selects MR1:0 (0) or MR3:2 (1).
    mr=r'(?:MR1:0|MR3:2|MR3:0|MR4)'
    m=re.fullmatch(r'('+R+r')\s*=\s*('+mr+r')\s*,\s*('+mr+r')\s*\+=\s*('+R+r')\s*(\*\*|\*)\s*('+R+r')',body)
    if m:
        d=reg(m[1],kinds=('',)); acc=m[2]
        require(acc==m[3] and acc!='MR4','MAC transfer must read and update the same accumulator')
        a=reg(m[4],kinds=('',)); b=reg(m[6],a.count,('',))
        require(d.count in (1,2) and a.count in (1,2),'MAC transfer width')
        require(options<={'U','I','C','CR','J'} and not {'C','CR'}<=options and not {'I','CR'}<=options,'MAC transfer options')
        complex_=m[5]=='**'; quad=acc=='MR3:0'
        require(not complex_ or a.count==1 and not quad and 'U'not in options,'complex MAC transfer shape')
        require('J'not in options or complex_,'J requires complex multiply')
        require(not quad or a.count==2 and d.count==2,'quad MAC transfer shape')
        require(a.count==1 or d.count==2,'paired MAC input requires paired result')
        require('CR'not in options or d.count==1 or quad,'paired MAC transfer cannot round')
        typ=(8+int(d.count==2)+2*int('J'in options)) if complex_ else 12 if a.count==2 else 4+int(d.count==2)
        code=16*int('U'in options)+8*int('I'in options)+4*int(bool({'C','CR'}&options))
        code+=(2+int('CR'in options)) if quad else 2*int('CR'in options)+int(acc=='MR3:2')
        return emit(3,typ,code,d.index,a.index,b.index,dests=(d,))
    m=re.fullmatch(r'([SL]?MR(?:1:0|3:2)|MR4)\s*=\s*('+R+r')',body)
    if m:
        key=opts(('', 'SE','ZE'))
        kind=m[1][0] if m[1][0] in 'SL' else ''
        is4=m[1]=='MR4'; a=reg(m[2],1 if is4 else 2,('',))
        require(not is4 or not key,'MR4 does not accept extension options')
        # A-8/10-152 '{S|L}MRa = Rmd {({SE|ZE}{NF})}': the option is optional;
        # C-17 has only the U-selected forms, so omission is SE (U=0).
        if kind and not key: key='SE'
        s=12 if is4 else {'':9,'S':10,'L':11}[kind] if key else 8
        code=16*int(key=='ZE')+int('3:2' in m[1])
        return emit(3,13,code,s,a.index)
    m=re.fullmatch(r'('+mr+r')\s*(\+=|-=)\s*('+R+r')\s*(\*\*|\*)\s*('+R+r')',body)
    if m:
        acc,operation,left,multiply,right=m.groups()
        require(acc!='MR4','MR4 is not an accumulator pair')
        a=reg(left,kinds=('',)); b=reg(right,a.count,('',)); require(a.count in (1,2),'MAC input width')
        require(options <= {'U','I','C','CR','J'},'invalid MAC option')
        require(not {'C','CR'}<=options and not {'I','CR'}<=options,'conflicting MAC options')
        complex_=multiply=='**'; sub=operation=='-='
        require(not complex_ or a.count==1 and acc!='MR3:0' and 'U'not in options,'complex MAC uses scalar operands and MR pair')
        require('J'not in options or complex_,'conjugate requires complex multiply')
        require(not sub or a.count==1 and acc!='MR3:0','subtract MAC is scalar only')
        require(acc!='MR3:0' or a.count==2,'quad MAC requires paired operands')
        require(a.count==1 or 'CR'not in options or acc=='MR3:0','dual MAC cannot round')
        s=(6 if 'J'in options else 4)+int(sub) if complex_ else 3 if acc=='MR3:0' else 2 if a.count==2 else int(sub)
        code=16*int('U'in options)+8*int('I'in options)+4*int(bool({'C','CR'}&options))+2*int('CR'in options)+int(acc=='MR3:2')
        return emit(3,13,code,s,a.index,b.index)
    m=re.fullmatch(r'('+R+r')\s*=\s*(COMPACT\s+)?([SL]?MR(?:1:0|3:2|3:0|[0-4]))',body)
    if m:
        d=reg(m[1]); compact=bool(m[2]); acc=m[3]
        require(options<={'U','S','I'},'invalid accumulator transfer option')
        require(compact or 'I'not in options,'I requires COMPACT')
        u=16*int('U'in options); sat=4*int('S'in options); code=u+sat
        if acc=='MR4':
            require(d.count==1 and d.kind=='' and not compact and not options,'MR4 transfer form')
            return emit(3,14,1,d.index,3,dests=(d,))
        single=re.fullmatch(r'(S?)MR([0-3])',acc)
        if single:
            require(not compact and d.count==2 and d.kind==('' if single[1] else 'L'),'single accumulator extraction width/type')
            require('S'not in options,'single accumulator extraction does not saturate')
            require(acc!='SMR3' or not options,'SMR3 does not accept U')
            return emit(3,14,u+int(single[2]),d.index,12 if single[1] else 13,dests=(d,))
        pair=int(acc.endswith('3:2'))
        if compact:
            require((d.kind=='' and d.count==1 and acc in ('MR1:0','MR3:2')) or
                    (d.kind=='S' and d.count==2 and acc=='MR3:0'),'COMPACT accumulator width/type')
            return emit(3,14,code+8*int('I'in options)+pair+2*int(acc=='MR3:0'),d.index,4,dests=(d,))
        if d.count==4:
            if acc=='MR3:0':
                require(d.kind=='','quad accumulator transfer result type')
                return emit(3,14,code,d.index,2,dests=(d,))
            require('S'not in options,'extended accumulator read cannot saturate')
            kind=acc[0] if acc[0] in 'SL' else ''
            require(d.kind=={'S':'','':'L','L':'Q'}[kind],'extended accumulator result type')
            return emit(3,14,u+pair,d.index,{'S':8,'':9,'L':10}[kind],dests=(d,))
        require(d.count==2 and d.kind in ('','S') and acc in ('MR1:0','MR3:2'),'accumulator transfer width/type')
        return emit(3,14,code+pair,d.index,int(d.kind=='S'),dests=(d,))

    # Shifter status/temporary register transfers and breakpoint filter.
    m=re.fullmatch(r'(STAT|STATL|STAT_L|BFOTMP)\s*=\s*('+R+r')',body)
    if m:
        # A-10 spells STAT_L, C-24 STATL.
        opts(); a=reg(m[2],2 if m[1]=='BFOTMP' else 1,('',))
        return emit(2,12,{'STAT':8,'STATL':14,'STAT_L':14,'BFOTMP':11}[m[1]],m=a.index)
    m=re.fullmatch(r'('+R+r')\s*=\s*(STAT|BFOTMP)',body)
    if m:
        opts(); d=reg(m[1],2 if m[2]=='BFOTMP' else 1,('',))
        return emit(2,12,9 if m[2]=='STAT' else 10,d.index,dests=(d,))
    m=re.fullmatch(r'BKFPT\s+('+R+r')\s*,\s*('+R+r')',body)
    if m:
        opts(); a=reg(m[1],2,('',)); b=reg(m[2],2,('',))
        return emit(2,12,15,m=a.index,n=b.index)
    m=re.fullmatch(r'('+R+r')\s*(=|\+=)\s*(FEXT|FDEP|MASK|GETBITS|PUTBITS)\s+('+R+r')\s+BY\s+('+R+r')',body)
    if m:
        d=reg(m[1]); operation=m[3]; a=reg(m[4],kinds=('',)); b=reg(m[5],kinds=('',))
        require((m[2]=='=')==(operation in ('FEXT','GETBITS')),'field operation assignment mismatch')
        require(options<=({'SE'} if operation in ('FEXT','GETBITS') else {'SE','ZF'} if operation=='FDEP' else set()),'field options')
        if operation in ('GETBITS','PUTBITS'):
            require(d.kind=='' and d.count==2 and a.count==(4 if operation=='GETBITS' else 2) and b.count==2,'bitstream operand widths')
            return emit(2,5 if operation=='GETBITS' else 6,16+int('SE'in options),d.index,a.index,b.index,dests=(d,))
        require(d.kind=='' and d.count==1 or d.kind=='L' and d.count==2,'field result word/long type')
        require(a.count==d.count and b.count in (1,2),'field operand widths')
        if operation=='MASK': require(b.count==d.count,'MASK control width')
        wide=int(d.count==2); typ=(1 if operation=='FEXT' else 2)+4*wide
        code=(8 if operation in ('FEXT','MASK') else 0)+4*int(b.count==2 and operation!='MASK')
        code+=int('SE'in options)+2*int('ZF'in options)
        return emit(2,typ,code,d.index,a.index|wide,b.index,True,(d,))

    # C-4 fixed data conversion. Destination/source prefixes determine lanes.
    m=re.fullmatch(r'('+R+r')\s*=\s*EXPAND\s+('+R+r')(?:\s*(\+|-)\s*('+R+r'))?',body)
    if m:
        d=reg(m[1]); a=reg(m[2],kinds=('S','B')); key=opts(('', 'I','IU'))
        require(d.kind==('' if a.kind=='S' else 'S') and d.count==2*a.count and a.count in (1,2),'EXPAND lane widths')
        code={'':0,'I':1,'IU':3}[key]
        if m[4]:
            b=reg(m[4],a.count,(a.kind,)); sub=m[3]=='-'
            op=(0 if a.kind=='S' else 8)+(4 if a.count==2 else 16*sub)+code
            return emit(1,0,op,d.index,a.index,b.index|int(sub and a.count==2),dests=(d,))
        return emit(1,0,28+code,d.index,a.index,2*int(a.kind=='B')+int(a.count==2),dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*COMPACT\s+('+R+r')(?:\s*(\+|-)\s*('+R+r'))?',body)
    if m:
        d=reg(m[1]); a=reg(m[2])
        if a.kind in ('L','Q'):
            key=opts(('', 'U','IS','ISU'))
            require(not m[4] and d.kind==('' if a.kind=='L' else 'L') and d.count*2==a.count,'wide COMPACT shape')
            return emit(1,3,12,d.index,a.index,4*int(a.kind=='Q')+{'':0,'U':1,'IS':2,'ISU':3}[key],dests=(d,))
        key=opts(('', 'I','T','IS','ISU'))
        require(d.count==1 and d.kind in ('S','B') and a.count==2 and a.kind==('' if d.kind=='S' else 'S'),'COMPACT lane widths')
        code={'':0,'I':1,'T':4,'IS':5,'ISU':7}[key]
        code+=8*int(d.kind=='B')+16*int(not m[4])
        b=reg(m[4],2,(a.kind,)).index|int(m[3]=='-') if m[4] else 0
        return emit(1,1,code,d.index,a.index,b,dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*MERGE\s+('+R+r')\s*,\s*('+R+r')',body)
    if m:
        opts(); d=reg(m[1]); a=reg(m[2],kinds=('',)); b=reg(m[3],a.count,('',))
        require(d.kind in ('S','B') and a.count in (1,2) and d.count==a.count*2,'MERGE lane widths')
        return emit(1,2,2*int(d.kind=='S')+int(a.count==2),d.index,a.index,b.index,dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*(SUM|ONES)\s+('+R+r')',body)
    if m:
        d=reg(m[1],1,('',)); a=reg(m[3]); ones=m[2]=='ONES'; key=opts(('',) if ones else ('','U'))
        require(a.count in (1,2) and a.kind in (('',) if ones else ('S','B')),'SUM/ONES source type')
        n=4 if ones else 2*int(a.kind=='B'); n+=int(a.count==2)
        return emit(1,2,4+2*int(key=='U'),d.index,a.index,n,dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*PR1:0',body)
    if m:
        opts(); d=reg(m[1],2,('',)); return emit(1,2,4,d.index,n=6,dests=(d,))
    m=re.fullmatch(r'PR1:0\s*=\s*('+R+r')',body)
    if m:
        opts(); a=reg(m[1],2,('',)); return emit(1,3,8,m=a.index,n=8)
    m=re.fullmatch(r'PR([01])\s*\+=\s*SUM\s+('+R+r')',body)
    if m:
        key=opts(('', 'U')); a=reg(m[2],kinds=('S','B')); require(a.count in (1,2),'SUM source width')
        return emit(1,3,8+2*int(key=='U'),m=a.index,n=int(m[1])*4+2*int(a.kind=='B')+int(a.count==2))
    m=re.fullmatch(r'PR([01])\s*\+=\s*ABS\s*\(\s*('+R+r')\s*-\s*('+R+r')\s*\)',body)
    if m:
        key=opts(('', 'U')); a=reg(m[2],2,('S','B')); b=reg(m[3],2,(a.kind,))
        return emit(1,3,4*int(a.kind=='B')+2*int(key=='U')+int(m[1]),m=a.index,n=b.index)
    m=re.fullmatch(r'('+R+r')\s*=\s*(VMAX|VMIN)\s*\(\s*('+R+r')\s*,\s*('+R+r')\s*\)',body)
    if m:
        opts(); d=reg(m[1],2,('S','B')); a=reg(m[3],2,('',)); b=reg(m[4],2,('',))
        return emit(0,13,24 if m[2]=='VMAX' else 28,d.index,a.index,b.index|int(d.kind=='S'),dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*BFOINC\s+('+R+r')',body)
    if m:
        opts(); d=reg(m[1],1,('',)); a=reg(m[2],2,('',))
        return emit(1,2,8,d.index,a.index,dests=(d,))
    m=re.fullmatch(r'('+R+r')\s*=\s*PERMUTE\s*\(\s*('+R+r')\s*,\s*(?:-\s*('+R+r')\s*,\s*)?('+R+r')\s*\)',body)
    if m:
        opts(); d=reg(m[1],4 if m[3] else 2,('',)); a=reg(m[2],2,('',)); b=reg(m[4],1,('',))
        require(not m[3] or m[3]==m[2],'PERMUTE negated source must match')
        return emit(1,12,20 if m[3] else 21,d.index,a.index,b.index,dests=(d,))
    return None
