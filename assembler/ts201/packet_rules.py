"""Conservative issue/port checks derived from PR 1-38..49.

These validate an explicit packet, not a schedule across packets. Dynamic memory
aliasing, pointer validity and peripheral protocols remain program obligations.
Register identities are (block, RF index) or ('ureg', universal index).
"""
from collections import Counter


SEQUENCER = {'branch','indirect','reduce','condition','flag','misc'}
# Headers in the C-38/C-40 formats: bit 15 is line NF, bit 30 of the other
# slots is CC (C-2, Table C-1).
NF_HEADERS = {'branch','indirect','reduce','condition','flag'}
DATA = {'memory','move','constant'}


def registers(group, index, width):
    if group in (4,6):
        width //= 2
    if group in (0,2,4,6,8):
        blocks = (1,) if group==0 else (2,) if group==2 else (1,2)
        return {(b,i) for b in blocks for i in range(index,index+width)}
    return {('ureg',group*32+i) for i in range(index,index+width)}


def validate(ops):
    from .assembler import require
    seq=[(i,op) for i,op in enumerate(ops) if op.kind in SEQUENCER]
    require(len(seq)<=1 and (not seq or seq[0][0]==0),'one sequencer instruction is allowed, in the first slot')
    require(all(op.kind in SEQUENCER|DATA|{'compute','alu','nop'} for op in ops),'unsupported parallel instruction class')

    # Constants and moves involving only compute/core registers may use either
    # IALU. Choose J first, then K; never move arithmetic or pointer operations.
    occupied=set()
    flexible=[]
    for op in ops:
        if op.kind not in DATA|{'alu'}: continue
        w=op.words[0]; group=(w>>21)&31
        can_move=(op.kind=='constant' and group not in (12,13,14,15)) or (
            op.kind=='move' and group not in (12,13,14,15) and ((w>>8)&63) not in (12,13,14,15))
        if can_move: flexible.append(op)
        else:
            require(op.unit not in occupied,'duplicate IALU resource in packet')
            occupied.add(op.unit)
    for op in flexible:
        available=[u for u in (0,1) if u not in occupied]
        require(available,'more than two IALU operations in packet')
        unit=available[0]; occupied.add(unit)
        op.words[0]=(op.words[0]&~(1<<28))|(unit<<28)
        if len(op.words)>1: op.words[1]=(op.words[1]&~(1<<30))|(unit<<30)
        op.unit=unit

    computes={b:[] for b in (1,2)}
    writes=set(); accesses=[]; ports=Counter(); single_ports=Counter(); dabs=set()
    cross_ialu=False; absolute_accesses=[]
    for op in ops:
        own=set(op.writes); reads=set(); w=op.words[0]
        if op.kind=='compute':
            for b,unit in op.resources:
                prior=computes[b]
                require(len(prior)<2,'more than two compute operations in one block')
                for other,other_unit in prior:
                    require(unit!=other_unit,'duplicate compute unit in packet')
                    require(not op.exclusive and not other.exclusive,'shifter input-port restriction requires exclusive compute block')
                    require(not ({unit,other_unit}=={'alu','mul'} and op.two_buses and other.two_buses),
                            'ALU and multiplier cannot both use two result buses')
                    require(not (unit=='clu' and op.two_buses and other_unit=='shift' or
                                 other_unit=='clu' and other.two_buses and unit=='shift'),
                            'quad CLU result conflicts with shifter result bus')
                prior.append((op,unit))
        elif op.kind=='alu':
            code=(w>>21)&31; dest=(w>>16)&31
            if code not in (12,13) and dest!=31:
                group=(14 if code in (8,9) else 12)+op.unit
                own |= registers(group,dest,1)
            if code in (2,3): own.add(('ureg',26*32))  # CJMP side effect
            no_flags=bool(ops[0].words[0]&(1<<15)) if ops[0].kind in ('condition','branch','indirect','reduce') else bool(w&(1<<30))
            if not no_flags: own.add(('ureg',(12+op.unit)*32+31))
        elif op.kind in DATA:
            width=1 if not w&(1<<14) else 4 if w&1 else 2
            source=[]; dest=[]
            if op.kind=='constant':
                group=(w>>21)&31
                dest=[(group,(w>>16)&31,1)]
            elif op.kind=='move':
                sg=((w>>21)&31)|(((w>>7)&1)<<5); dg=(w>>8)&63
                source=[(sg,(w>>16)&31,width)]; dest=[(dg,(w>>1)&31,width)]
            else:
                group=(w>>21)&31; alternate=group<10 and group&1
                data_group=group-1 if alternate else group
                triple=(data_group,(w>>16)&31,width)
                if w&(1<<26): source=[triple]
                else: dest=[triple]
                if alternate and ((w>>10)&7)>=2:
                    active={b for b,_ in registers(data_group,0,width)}
                    require(not active&dabs,'two DAB loads target the same compute block')
                    dabs |= active
                if w&(1<<15): own |= registers(12+op.unit,(w>>8)&(3 if alternate else 31),1)
                if not alternate and ((w>>8)&31)==31 and w&(1<<13):
                    low=w&255
                    address=((op.words[1]&0xFFFFFF)<<8)|low if len(op.words)>1 else low if low<128 else low-256
                    if width>1: address &= ~1
                    store=bool(w&(1<<26))
                    for other_address,other_width,other_store in absolute_accesses:
                        require(not (store or other_store) or address+width<=other_address or other_address+other_width<=address,
                                'parallel absolute memory accesses overlap with a store')
                    absolute_accesses.append((address,width,store))
            per_op_single=set()
            for reading,transfers in ((True,source),(False,dest)):
                for group,index,width in transfers:
                    identities=registers(group,index,width)
                    if reading: reads |= identities
                    else: own |= identities
                    if group in (0,2,4,6,8):
                        for b in {b for b,_ in identities}: ports[b,'out' if reading else 'in']+=1
                    else:
                        port='j' if group in (12,14) else 'k' if group in (13,15) else 'seq' if group==26 or 48<=group<=59 else 'debug' if group==27 or group>=61 else 'soc' if 32<=group<=39 else group
                        per_op_single.add(port)
                        if group in (12,13,14,15) and (group&1)!=op.unit: cross_ialu=True
            single_ports.update(per_op_single)
        elif op.kind=='branch' and w&(1<<23) or op.kind=='indirect' and w&(1<<5):
            own.add(('ureg',26*32))
        elif op.kind=='flag':
            own.add(('ureg',26*32+28))  # SFREG contains the static flags.
        if op.kind in ('condition','branch','indirect','reduce'):
            code=(w>>16)&63
            if code in (12,13): own.add(('ureg',26*32+8+code-12))
        require(not writes&own,'overlapping result registers or sequencer side effects in packet')
        writes |= own
        accesses.append((own,reads))
    for b in (1,2):
        require(ports[b,'out']<=1 and ports[b,'in']<=2,'compute register transfer port conflict')
    # PR 1-35 Table 1-1 note 4: one SOC Ureg (groups 0x20..0x27) transfer
    # per line; core Uregs allow two.
    require(single_ports['soc']<=1,'only one SOC register transfer per instruction line (PR 1-35 note 4)')
    require(all(n<=1 for n in single_ports.values()),'multiple accesses to a single-port universal register file')
    require(not cross_ialu or not any(op.kind=='constant' for op in ops),
            'cross-IALU transfer cannot share a packet with an immediate load (PR 1-48)')
    # Deliberately reject store/transfer source overlap rather than silently
    # depending on bypass ordering. Independent compute source reads are legal.
    for i,(_,reads) in enumerate(accesses):
        require(not any(reads&other_writes for j,(other_writes,_) in enumerate(accesses) if i!=j),
                'parallel store/transfer reads a register written in the same packet')


def effects(op):
    """Conservative (reads, writes, accesses) of one decoded operation, for
    tools that move operations between lines. Register identities follow
    registers(); ('state', b) stands for everything else a compute block
    holds (flags, accumulators, CLU/special registers) and ('ureg', g*32+31)
    for JSTAT/KSTAT. A compute operation reads and writes its whole block.
    accesses lists (base, offset, width, store) memory references with base
    ('ureg', register) and a literal word offset, or offset None when the
    address is not a known short literal (extension, index register,
    CB/BR/DAB). Returns None for kinds it does not model (sequencer)."""
    w = op.words[0]
    if op.kind == 'nop':
        return set(), set(), []
    if op.kind == 'compute':
        blocks = {b for b, _ in op.resources} or {1, 2}
        state = {(b, i) for b in blocks for i in range(32)} | {('state', b) for b in blocks}
        return state, set(state), []
    if op.kind == 'alu':
        unit = op.unit
        code, dest = (w >> 21) & 31, (w >> 16) & 31
        ialu = lambda i: ('ureg', (12 + unit) * 32 + i)
        reads = {ialu((w >> 8) & 31), ialu(31)}
        if not w & 1 << 13:
            reads.add(ialu((w >> 1) & 31))
        if code in (4, 5, 6, 7, 8, 9):  # CB/BR address modes, base/length destinations
            reads |= {('ureg', (14 + unit) * 32 + i) for i in range(8)}
        writes = {ialu(31)}
        if code not in (12, 13) and dest != 31:
            writes |= registers((14 if code in (8, 9) else 12) + unit, dest, 1)
        if code in (2, 3):
            writes.add(('ureg', 26 * 32))
        return reads, writes, []
    width = 1 if not w & 1 << 14 else 4 if w & 1 else 2
    if op.kind == 'constant':
        return set(), registers((w >> 21) & 31, (w >> 16) & 31, 1), []
    if op.kind == 'move':
        source = ((w >> 21) & 31) | ((w >> 7) & 1) << 5
        return registers(source, (w >> 16) & 31, width), registers((w >> 8) & 63, (w >> 1) & 31, width), []
    if op.kind == 'memory':
        unit, store = op.unit, bool(w & 1 << 26)
        group = (w >> 21) & 31
        alternate = group < 10 and group & 1
        index = (w >> 8) & (3 if alternate else 31)
        # J31/K31 read as zero, not as JSTAT/KSTAT.
        base = ('zero',) if index == 31 else ('ureg', (12 + unit) * 32 + index)
        data = registers(group - 1 if alternate else group, (w >> 16) & 31, width)
        reads, writes = ({base} if index != 31 else set()), set()
        (reads if store else writes).update(data)
        offset = None
        if w & 1 << 13:
            if len(op.words) == 1 and not alternate:
                # Bit 0 marks a quad; wide literal offsets are aligned.
                raw = w & (254 if width > 1 else 255)
                offset = 0 if w & 1 << 15 else raw - (256 if raw & 128 else 0)
        else:
            reads.add(('ureg', (12 + unit) * 32 + ((w >> 1) & 31)))
        if alternate:
            reads |= {('ureg', (14 + unit) * 32 + i) for i in range(8)}
        if w & 1 << 15:
            writes.add(base)
        return reads, writes, [(base, offset, width, store)]
    if op.kind == 'branch':
        return set(), {('ureg', 26 * 32)} if w & 1 << 23 else set(), []
    return None
