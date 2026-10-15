"""Semantic expectations computed independently of the assembler."""
import random
import struct


def expansion_corpus():
    code=['.entry start','.section text, code, 0x100','start: J0 = results;;']
    expected=[]
    def bits(v): return struct.unpack('<I',struct.pack('<f',v))[0]
    def case(source,left,right,result):
        code.extend([f'XR1 = {left & 0xffffffff};;',f'XR2 = {right & 0xffffffff};;',
                     'X '+source+';;','[J0 += 1] = XR0;;'])
        expected.append(result & 0xffffffff)
    rng=random.Random(201)
    values=[(0,0),(-1,1),(0x7fffffff,1),(-0x80000000,-1),(123456,-12345)]
    values += [(rng.randrange(-100000,100000),rng.randrange(-100000,100000)) for _ in range(12)]
    signed=lambda v:(v+0x80000000)%0x100000000-0x80000000
    for a,b in values:
        for source,result in [
            ('R0 = R1 + R2',a+b),('R0 = R1 - R2',a-b),
            ('R0 = MIN(R1,R2)',min(a,b)),('R0 = MAX(R1,R2)',max(a,b)),
            ('R0 = R1 AND R2',a&b),('R0 = R1 AND NOT R2',a&~b),
            ('R0 = R1 OR R2',a|b),('R0 = R1 XOR R2',a^b),
            ('R0 = NOT R1',~a),('R0 = ABS R1',abs(a)),('R0 = -R1',-a),
            ('R0 = R1 * R2 (I)',a*b),
            ('R0 = R1 + R2 (S)',max(-0x80000000,min(0x7fffffff,a+b))),
            ('R0 = R1 - R2 (S)',max(-0x80000000,min(0x7fffffff,a-b))),
            ('R0 = INC R1',a+1),('R0 = DEC R1',a-1),
        ]: case(source,a,b,result)
        for shift in (-31,-1,0,1,17,31):
            logical=(a&0xffffffff)<<shift if shift>=0 else (a&0xffffffff)>>-shift
            arithmetic=a<<shift if shift>=0 else a>>-shift
            n=shift%32; u=a&0xffffffff
            rotation=((u<<n)|(u>>(32-n)))&0xffffffff
            case(f'R0 = LSHIFT R1 BY {shift}',a,0,logical)
            case(f'R0 = ASHIFT R1 BY {shift}',a,0,arithmetic)
            case(f'R0 = ROT R1 BY {shift}',a,0,rotation)
        for bit in (0,7,31):
            case(f'R0 = BSET R1 BY {bit}',a,0,a|(1<<bit))
            case(f'R0 = BCLR R1 BY {bit}',a,0,a&~(1<<bit))
            case(f'R0 = BTGL R1 BY {bit}',a,0,a^(1<<bit))
    for a,b in [(1.5,2.25),(-7.0,0.5),(0.0,3.0),(16.0,-2.0),(123.25,4.0)]:
        for source,result in [('FR0 = R1 + R2',a+b),('FR0 = R1 - R2',a-b),
                              ('FR0 = R1 * R2',a*b),('FR0 = MIN(R1,R2)',min(a,b)),
                              ('FR0 = MAX(R1,R2)',max(a,b)),('FR0 = ABS R1',abs(a)),
                              ('FR0 = -R1',-a)]:
            case(source,bits(a),bits(b),bits(result))
    code.extend(['done: IDLE;;','.section data, data, 0x40000',f'results: .zero {len(expected)}'])
    return '\n'.join(code),dict(max_packets=10000,idle_symbol='done',registers={},memory={'results':expected})
