"""Independent Appendix C field worksheets, not encoder-generated expectations.

Each row fixes CU, TYPE, OPCODE, RS, RM and RN directly from the cited table.
Changing a parser's rule does not change the expected row. The test expands X/Y
selection and NF around those fixed fields; semantic tests are separate.
"""
import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program

# body, CU, TYPE, OPCODE, RS, RM, RN, alternate. Table C-3/4/5/7..11.
ROWS = [
    ('R4 = ABS(R2 + R3) (X)',0,0,9,4,2,3,False),
    ('R4 = ABS(R2 - R3) (U)(X)',0,0,15,4,2,3,False),
    ('R4 = (R2 + R3)/2 (T)(U)',0,0,19,4,2,3,False),
    ('R4 = (R2 - R3)/2',0,0,20,4,2,3,False),
    ('R4 = MIN(R2,R3) (U)(Z)',0,0,31,4,2,3,False),
    ('R4 = R2 + R3 + CI (S)',0,1,1,4,2,3,False),
    ('R4 = R2 - R3 + CI - 1',0,1,4,4,2,3,False),
    ('R4 = CLIP R2 BY R3',0,1,20,4,2,3,False),
    ('R4 = R2 + CI',0,1,28,4,2,0,False),
    ('R4 = R2 + CI - 1',0,1,29,4,2,0,False),
    ('R4 = R2 + R3, R5 = R2 - R3',0,2,5,4,2,3,False),
    ('SR5:4 = VMAX(R3:2,R7:6)',0,13,24,4,2,7,False),
    ('BR5:4 = VMIN(R3:2,R7:6)',0,13,28,4,2,6,False),
    ('R5:4 = EXPAND SR2 (I)',1,0,29,4,2,0,False),
    ('SR7:4 = EXPAND BR3:2 (IU)',1,0,31,4,2,3,False),
    ('R7:4 = EXPAND SR3:2 - SR7:6 (I)',1,0,5,4,2,7,False),
    ('SR5:4 = EXPAND BR2 - BR3 (IU)',1,0,27,4,2,3,False),
    ('SR4 = COMPACT R3:2 - R7:6 (IS)',1,1,5,4,2,7,False),
    ('BR4 = COMPACT SR3:2 + SR7:6 (T)',1,1,12,4,2,6,False),
    ('BR4 = COMPACT SR3:2 (ISU)',1,1,31,4,2,0,False),
    ('R4 = COMPACT LR3:2 (ISU)',1,3,12,4,2,3,False),
    ('LR5:4 = COMPACT QR7:4 (U)',1,3,12,4,4,5,False),
    ('SR5:4 = MERGE R2,R3',1,2,2,4,2,3,False),
    ('BR7:4 = MERGE R3:2,R7:6',1,2,1,4,2,6,False),
    ('R4 = SUM SR3:2 (U)',1,2,6,4,2,1,False),
    ('R4 = SUM BR2',1,2,4,4,2,2,False),
    ('R4 = ONES R3:2',1,2,4,4,2,5,False),
    ('R5:4 = PR1:0',1,2,4,4,0,6,False),
    ('PR1:0 = R3:2',1,3,8,0,2,8,False),
    ('PR1 += SUM BR3:2 (U)',1,3,10,0,2,7,False),
    ('PR0 += ABS(SR3:2-SR7:6)',1,3,0,0,2,6,False),
    ('PR1 += ABS(BR3:2-BR7:6) (U)',1,3,7,0,2,6,False),
    ('R4 = BFOINC R3:2',1,2,8,4,2,0,False),
    ('R5:4 = PERMUTE(R3:2,R7)',1,12,21,4,2,7,False),
    ('R7:4 = PERMUTE(R3:2,-R3:2,R7)',1,12,20,4,2,7,False),
    ('FR4 = (R2 + R3)/2 (T)',1,4,10,4,2,3,False),
    ('FR5:4 = ABS(R3:2-R7:6)',1,4,21,4,2,6,False),
    ('FR5:4 = FLOAT R2 BY R3 (T)',1,4,27,4,2,3,False),
    ('R4 = FIX FR3:2 BY R7 (T)',1,4,31,4,2,7,False),
    ('FR4 = R2 COPYSIGN R3',1,5,2,4,2,3,False),
    ('FR5:4 = SCALB FR3:2 BY R7',1,5,5,4,2,7,False),
    ('R4 = MANT FR3:2',1,5,9,4,2,2,False),
    ('R4 = LOGB FR3:2 (S)',1,5,11,4,2,8,False),
    ('FR4 = RECIPS R2',1,5,8,4,2,5,False),
    ('FR5:4 = RSQRTS R3:2',1,5,9,4,2,6,False),
    ('FR5:4 = EXTD R2',1,5,9,4,2,9,False),
    ('FR4 = SNGL R3:2 (T)',1,5,10,4,2,10,False),
    ('FR4 = R2 + R3, FR5 = R2 - R3',1,6,5,4,2,3,False),
    ('FCOMP(R3:2,R7:6)',1,5,17,0,2,6,False),
    ('R5:4 = R2 * R3 (I)(NU)',3,1,13,4,2,3,False),
    ('R5:4 = R3:2 * R7:6 (I)(U)',3,2,24,4,2,6,False),
    ('R7:4 = R3:2 * R7:6 (I)',3,3,12,4,2,6,False),
    ('MR3:2 += R2 * R3 (I)(C)',3,13,13,0,2,3,False),
    ('MR1:0 -= R2 * R3 (U)(CR)',3,13,22,1,2,3,False),
    ('MR1:0 += R3:2 * R7:6 (I)',3,13,8,2,2,6,False),
    ('MR3:0 += R3:2 * R7:6 (CR)',3,13,6,3,2,6,False),
    ('MR3:2 -= R2 ** R3 (J)(I)',3,13,9,7,2,3,False),
    ('R4 = MR3:2, MR3:2 += R2 * R3 (CR)',3,4,7,4,2,3,False),
    ('R5:4 = MR1:0, MR1:0 += R2 * R3 (I)',3,5,8,4,2,3,False),
    ('R4 = MR1:0, MR1:0 += R2 ** R3 (J)(C)',3,10,4,4,2,3,False),
    ('R5:4 = MR3:2, MR3:2 += R2 ** R3',3,9,1,4,2,3,False),
    ('R5:4 = MR1:0, MR1:0 += R3:2 * R7:6 (I)',3,12,8,4,2,6,False),
    ('R5:4 = MR3:0, MR3:0 += R3:2 * R7:6 (CR)',3,12,7,4,2,6,False),
    ('R5:4 = MR3:0, MR3:0 += R3:2 * R7:6 (I)(C)',3,12,14,4,2,6,False),
    ('MR1:0 = R3:2',3,13,0,8,2,0,False),
    ('MR3:2 = R3:2 (ZE)',3,13,17,9,2,0,False),
    ('SMR1:0 = R3:2 (SE)',3,13,0,10,2,0,False),
    ('LMR3:2 = R3:2 (ZE)',3,13,17,11,2,0,False),
    ('MR4 = R2',3,13,0,12,2,0,False),
    ('R5:4 = MR3:2 (U)(S)',3,14,21,4,0,0,False),
    ('SR5:4 = MR1:0',3,14,0,4,1,0,False),
    ('R7:4 = MR3:0',3,14,0,4,2,0,False),
    ('R4 = MR4',3,14,1,4,3,0,False),
    ('R4 = COMPACT MR3:2 (I)(U)(S)',3,14,29,4,4,0,False),
    ('SR5:4 = COMPACT MR3:0',3,14,2,4,4,0,False),
    ('R7:4 = SMR1:0',3,14,0,4,8,0,False),
    ('LR7:4 = MR3:2',3,14,1,4,9,0,False),
    ('QR7:4 = LMR1:0',3,14,0,4,10,0,False),
    ('R5:4 = SMR2 (U)',3,14,18,4,12,0,False),
    ('LR5:4 = MR3 (U)',3,14,19,4,13,0,False),
    ('R4 = FEXT R2 BY R3 (SE)',2,1,9,4,2,3,True),
    ('LR5:4 = FEXT R3:2 BY R7:6',2,5,12,4,3,6,True),
    ('R4 += FDEP R2 BY R3 (SE)(ZF)',2,2,3,4,2,3,True),
    ('LR5:4 += MASK R3:2 BY R7:6',2,6,8,4,3,6,True),
    ('R5:4 = GETBITS R7:4 BY R9:8 (SE)',2,5,17,4,4,8,False),
    ('R5:4 += PUTBITS R3:2 BY R7:6',2,6,16,4,2,6,False),
    ('STAT = R2',2,12,8,0,2,0,False),
    ('STATL = R2',2,12,14,0,2,0,False),
    ('R4 = STAT',2,12,9,4,0,0,False),
    ('R5:4 = BFOTMP',2,12,10,4,0,0,False),
    ('BFOTMP = R3:2',2,12,11,0,2,0,False),
    ('BKFPT R3:2,R7:6',2,12,15,0,2,6,False),
]


class TableTests(unittest.TestCase):
    def test_manual_field_rows(self):
        for body,cu,typ,op,s,m,n,alternate in ROWS:
            for block,bits in [('X',0x24000000),('Y',0x28000000),('XY',0x2c000000)]:
                if block=='XY' and 'STAT' in body:
                    continue  # A-10: {X|Y}STAT only; rejection tested in test_manual_syntax
                with self.subTest(body=body,block=block):
                    wanted=0x80000000|bits|cu<<24|typ<<20|op<<15|s<<10
                    wanted|=(n<<5|m) if alternate else (m<<5|n)
                    self.assertEqual(assemble(program(f'{block} {body};;')).sections[0].words,[wanted])

    def test_accumulator_conversion_rejections(self):
        for body in ['MR4 += R0 * R1','MR1:0 += R1:0 * R3:2 (CR)',
                     'MR3:0 += R0 * R1','R0 = MR1:0','R1:0 = MR4',
                     'MR1:0 = R0','MR4 = R0 (SE)',
                     'R0 = COMPACT R1:0','BR0 = COMPACT R1:0',
                     'R1:0 = EXPAND BR0','R1:0 = MERGE R2,R3',
                     'LR1:0 = FEXT R0 BY R1','R0 = FDEP R1 BY R2',
                     'R0 += FEXT R1 BY R2','R0 = SUM R1','R0 = ONES BR1']:
            with self.subTest(body=body),self.assertRaises(AssemblyError):
                assemble(program('X '+body+';;'))

    def test_clu_literal_vectors(self):
        vectors={
            'X R0 = TR0;;':0xB8000000,
            'Y R0 = TR0;;':0xB4000000,
            'XY R3:0 = THR3:0;;':0xBC008060,
            'X R0 = CMCTL;;':0xB800801F,
            'X TR0 = R0;;':0xA6F00000,
            'X THR3:0 = R7:4 (I);;':0xA6F00C98,
            'X CMCTL = R0;;':0xA6FF8800,
            'X R4 = TMAX(TR2,TR3);;':0xB8309062,
            'X TR1:0 = MAX(TR3:2 + R7:4_H, TR5:4 + R7:4_L);;':0xB8201083,
            'X TR0 += DESPREAD(R3:0,THR1:0);;':0xB8800000,
            'X R7:4 = TR19:16, TR11:8 = ACS(TR3:2,TR5:4,R6);;':0xB93410C3,
            'X TR31:16 = XCORRS(R7:4,THR7:4)(CUT -1)(CLR)(EXT);;':0xB8E188E7,
            'Y TR15:0 = XCORRS(R3:0,THR3:0)(CUT R);;':0xB4C80400,
        }
        for source,wanted in vectors.items():
            with self.subTest(source=source):
                self.assertEqual(assemble(program(source)).sections[0].words,[wanted])
