"""Manual spellings the assembler must accept (PR Rev 1.1).

Unprefixed compute registers mean both blocks: 2-7 "XY (or no prefix) =
both", 7-10 broadcast loads, Listing 8-1 'IF ALE; DO, R0 = R1 + R2'. Compute
conditions without a block prefix are XY (C-44 <XY>=11, 3-15 Table 3-3,
10-234 'IF AEQ, JUMP label1'), as are static flags (3-15 'SF0').
"""
import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program


def words(text):
    return assemble(program(text+';;')).sections[0].words


class UnprefixedSyntaxTests(unittest.TestCase):
    def test_literal_vectors(self):
        for text,wanted in {
            'R3 = R1 + R2':[0xAC000C22],                    # XY=11 (C-3 Fig C-2)
            'LR5:4 = R11:10 + R1:0':[0xAC301141],           # 3-16 example, type 0011, Rn LSB=1
            'IF ALE; DO, R0 = R1 + R2':[0x33720000,0xEC000022],   # cond <XY>=11 ALE=0x32
            'IF AEQ, JUMP start':[0x71300000,0xB3800000],   # 10-234 example
            'SF0 = AEQ':[0xB3300700],                       # SCF sel <xy>0=110, op 001
        }.items():
            with self.subTest(text=text):
                self.assertEqual(words(text),wanted)

    def test_same_as_xy(self):
        for text in ['R3 = R1 + R2','FR0 = PASS R1','FR1:0 = R3:2 * R5:4','SR1:0 = R3:2 + R5:4',
                     'MR1:0 += R0 * R1 (I)','R1:0 = MR1:0','MR4 = R0','COMP(R1, R2) (U)',
                     'SCOMP(R1:0, R3:2)','FCOMP(R1, R2)','BITEST R1 BY 3','BKFPT R1:0, R3:2',
                     'R0 = LSHIFT R1 BY 3','R1 += FDEP R2 BY R3','PR1:0 = R1:0','R1:0 = PR1:0',
                     'PR0 += SUM SR1:0','TR0 = R0','R0 = TR0','THR1:0 = R1:0 (I)','CMCTL = R0',
                     'R0 = CMCTL','BFOTMP = R1:0','R1:0 = BFOTMP','TR3:2 = MAX(TR1:0 + R7:4_H, TR5:4 + R7:4_L)',
                     'R0 = [J0 + 1]','R3:0 = Q[J0 + 4]','R1:0 = L[K1 += 2]','R3:0 = DAB Q[J0 += 4]',
                     'R0 = 5','R0 = 0x12345678','R3:0 = CB Q[J1 += 4]']:
            with self.subTest(text=text):
                self.assertEqual(words(text),words('XY'+text if text.startswith(('R','[')) else 'XY '+text))

    def test_conditions_and_flags(self):
        for plain,xy in [('IF ALE; DO, R0 = R1 + R2','IF XYALE; DO, XYR0 = R1 + R2'),
                         ('IF NMEQ, JUMP start (NP)','IF NXYMEQ, JUMP start (NP)'),
                         ('IF SF1; DO J0 = 1','IF XYSF1; DO J0 = 1'),
                         ('SF1 += OR SLT','XYSF1 += OR XYSLT'),
                         ('ISF0 = AEQ','ISF0 = XYAEQ')]:
            with self.subTest(text=plain):
                self.assertEqual(words(plain),words(xy))

    def test_prefixed_forms_unchanged(self):
        self.assertEqual(words('COMP(J1, J2)'),[0x8D800104])     # IALU, not compute
        self.assertEqual(words('XR3 = R1 + R2'),[0xA4000C22])

    def test_still_rejected(self):
        for text in ['[J0 + 1] = R0',            # broadcast is load-only (A-15/A-16)
                     'STAT = R0','R0 = STAT',    # {X|Y}STAT only (A-10)
                     'XY STAT = R0','XY STATL = R0','XYR0 = STAT',
                     'R0 = R1 + J2','R32 = R1 + R2','R0 = J1']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                words(text)


class TypedResultSpellingTests(unittest.TestCase):
    def test_dual_add_subtract_second_result(self):
        # A-3: '{S|B}Rs = Rm + Rn, Ra = Rm - Rn' - Ra carries no prefix.
        self.assertEqual(words('XSR1 = R2 + R3, R4 = R2 - R3'),words('XSR1 = R2 + R3, SR4 = R2 - R3'))
        self.assertEqual(words('XLR1:0 = R3:2 + R5:4, R7:6 = R3:2 - R5:4'),
                         words('XLR1:0 = R3:2 + R5:4, LR7:6 = R3:2 - R5:4'))
        with self.assertRaises(AssemblyError):
            words('XSR1 = R2 + R3, BR4 = R2 - R3')

    def test_stat_l(self):
        # A-10 '{X|Y}STAT_L = Rm'; C-24 'X/YSTATL = Rm'.
        self.assertEqual(words('XSTAT_L = R1'),words('XSTATL = R1'))


class CluMaxSpellingTests(unittest.TestCase):
    def test_manual_pair_operands(self):
        # PR 10-97/10-99 examples: Rmq_h/Rmq_l written as the two pairs.
        self.assertEqual(words('XYTR1:0 = TMAX(TR3:2 + R3:2, TR5:4 + R1:0)'),[0xBC281003])
        self.assertEqual(words('XYTR1:0 = MAX(TR3:2 - R7:6, TR5:4 - R5:4)'),
                         words('XYTR1:0 = MAX(TR3:2 - R7:4_H, TR5:4 - R7:4_L)'))
        for text in ['XYTR1:0 = MAX(TR3:2 - R5:4, TR5:4 - R7:6)',
                     'XYTR1:0 = MAX(TR3:2 - R7:6, TR5:4 - R3:2)',
                     'XYTR1:0 = MAX(TR3:2 - R7:6, TR5:4 + R5:4)']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                words(text)


class ManualExampleSpellingTests(unittest.TestCase):
    def test_equivalent_spellings(self):
        for manual,canonical in [
            ('IF XSF0, DO XR5 = R4 + R3','IF XSF0; DO XR5 = R4 + R3'),          # 3-16
            ('IF ISF0, DO J5 = J4 + J3','IF ISF0; DO J5 = J4 + J3'),            # 7-14
            ('XYFR2 = ( R1 + R0 ) / 2 ()','XYFR2 = (R1 + R0) / 2'),            # 3-7
            ('XR0 = FIX XFR3:2','XR0 = FIX FR3:2'),                            # 10-70
            ('YR0 = FIX YFR1 BY R2 (T)','YR0 = FIX FR1 BY R2 (T)'),            # 10-70
            ('XR0 = LOGB XFR1 (S)','XR0 = LOGB FR1 (S)'),                      # 10-93
            ('XR2 += MASK XR0 BY XR1','XR2 += MASK R0 BY R1'),                 # 10-181
            ('XYR0 = XYR1 + XYR2','XYR0 = R1 + R2'),
            ('XR3:0 = R7:4','XR3:0 = XR7:4'),                                  # 1-45 Ureg move
        ]:
            with self.subTest(text=manual):
                self.assertEqual(words(manual),words(canonical))

    def test_parallel_ureg_move_examples(self):
        # PR 1-45: valid / invalid pairs of a compute Ureg move with a load/store.
        words('XR3:0 = R7:4 ; [J17 + 2] = YR4')
        words('XR3:0 = R7:4 ; XR10 = [J17 + 2]')
        words('R3:0 = R31:28')
        for text in ['XR3:0 = R7:4 ; [J17 + 2] = XR4','R3:0 = R31:28 ; [J17 + 2] = YR8',
                     'XR0 = FIX YFR1','XR0 = R1 + XYR2']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                words(text)


class ManualExampleSemanticsTests(unittest.TestCase):
    def test_vectors(self):
        for text,wanted in {
            # 8-95/C-42: long form cannot fit -> one-slot 15-bit branch (C-39).
            'IF NLC0E, JUMP start; NOP; NOP; NOP':[0x714C0000,0x33C00000,0x33C00000,0xB3C00000],
            'JUMP start; J0 = 0x12345678':[0x71080000,0x32123456,0x8980D678],
            'JUMP start (ABS); NOP; NOP; NOP':[0x70080100,0x33C00000,0x33C00000,0xB3C00000],
            # 8-93 'CJMP = ADDRESS(test)': Ureg load of CJMP, not a branch.
            'CJMP = start':[0x0B408100,0xB2000001],
            'CJMP = ADDRESS(start)':[0x0B408100,0xB2000001],
            # 10-86/10-88: float immediates load IEEE single bits.
            'XR0 = 9.0':[0x08008000,0xB2411000],
            'XR1 = 0.5':[0x08018000,0xB23F0000],
            'YR0 = -2.0':[0x08408000,0xB2C00000],
            # 10-198 'XYTR1:0 = R3:2': Rm is the standard 9:5 field.
            'XYTR1:0 = R3:2':[0xAEF00050],
        }.items():
            with self.subTest(text=text):
                self.assertEqual(words(text),wanted)

    def test_equivalences(self):
        # A-17: 'CACMDx = Jmd; /* long word transfer */'.
        self.assertEqual(words('CACMDB = J1:0'),words('CACMADRB = J1:0'))
        self.assertEqual(words('CACMD0 = J3:2'),words('CACMADR0 = J3:2'))
        # 8-16/10-242: predicted PC-relative CJMP.
        words('IF MEQ, CJMP; ELSE R4 = R6 + R7')
        words('XYTHR1:0 = R3:2 (I)')

    def test_rejections(self):
        for text in ['CJMP_CALL','XYTR1:0 = R2:1','CACMDB = J3:0','XR0 = 1e40']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                words(text)
        with self.assertRaisesRegex(AssemblyError,'below 0x8000'):
            assemble(program('JUMP start (ABS); NOP; NOP; NOP;;',base=0x40000))


class OptionListingTests(unittest.TestCase):
    def test_listing_options(self):
        # A-8/10-152 '{S|L}MRa = Rmd {({SE|ZE}{NF})}': omitted option = SE (U=0, C-17).
        self.assertEqual(words('X SMR1:0 = R1:0'),words('X SMR1:0 = R1:0 (SE)'))
        self.assertEqual(words('X LMR3:2 = R1:0'),words('X LMR3:2 = R1:0 (SE)'))
        self.assertNotEqual(words('X MR1:0 = R1:0'),words('X MR1:0 = R1:0 (SE)'))
        # A-8 'MR4 = Rm {(NF)}' (5-22 prose disagrees; listing followed).
        words('X MR4 = R0 (NF)')
        for text in ['X R0 = MR4 (NF)',               # A-8 'Rs = MR4 ;'
                     'X MR1:0 += R0 * R1 (I)(CR)',    # 5-17: CR only for fractional
                     'X R0 = MR1:0, MR1:0 += R0 * R1 (I)(CR)']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                words(text)
        # Transfer-shaped text reports the transfer error, not an ALU one.
        with self.assertRaisesRegex(AssemblyError,'J0..3'):
            words('R11:8 = XDAB Q [J4 += J12]')


class ParallelismTableTests(unittest.TestCase):
    def test_soc_single_transfer(self):
        # PR 1-35 Table 1-1 note 4: SOC Ureg transfers are one per line.
        for text in ['DCNT = J0; SYSCON = K0','DCNT = J0; DCNT = K0','J1 = SYSCON; K1 = DCNT']:
            with self.subTest(text=text),self.assertRaisesRegex(AssemblyError,'SOC|single-port|overlapping'):
                words(text)
        words('DCNT = J0; K0 = K1')
        words('J0 = J1; K0 = K1')      # core Uregs: two transfers allowed

    def test_chapter1_labelled_examples(self):
        # PR 1-38..1-48 examples with their printed Valid/Invalid labels.
        valid=['R0 = R1 + R2 ; R3 = R4 * R5','XR0 = R1 + R2 ; YR0 = R1 + R2',
               'R0 = R1 + R2 , R5 = R1 - R2 ; XR6 = R1 * R2','R3:0 = DAB Q[J0 += 4]',
               'R1:0 = R11:10','R0 = [J0 + 1] ; R1 = [K1 + 1]','R0 = [J0 + 1] ; [K1 + 1] = XR1',
               'XR3:0 = R7:4 ; [J17 + 2] = YR4','XR3:0 = R7:4 ; XR10 = [J17 + 2]','R3:0 = R31:28',
               'XR3:0 = DAB Q[J0 += 4] ; YR7:4 = DAB Q[K0 += 4]','J0 = [J5 + 1] ; K0 = [K6 + 1]',
               'J0 = [J5 + 1] ; [K6 + 1] = K0','XR3:0 = Q[J0 += 4]','Q[K2 + 0] = K3:0 ; XR0 = 100',
               'XR0 = R5 + R6 ; J0 = J2 + J3 ; YR4 = [K3 + 40]']
        invalid=['XR0 = R1 + R2 ; XR0 = R5 * R6','XR1 = R2 + R3 , XR1 = R2 - R3',
                 'XR0 = [J17 + 1] ; R0 = R3 * R8','J0 = [J0 += 1]',
                 'IF ALE, CALL start ; J6 = J0 + J1 (CJMP)',
                 'XR0 = R1 + R2 ; XR3 = R4 * R5 ; XR6 = LSHIFT R1 BY R7','XR0 = R1 + R2 ; XR6 = R1 + R2',
                 'XR0 += MASK R1 BY R2 ; XR6 = R1 + R2','R0 = R1 + R2 , R5 = R1 - R2 ; XR11:8 = MR3:0',
                 'R3:0 = THR3:0; R6 = LSHIFT R1 BY R7','XR0 = YR0 ; XR1 = [J0 += 1] ; YR1 = [K0 += 1]',
                 'XR0 = [J0 + 1] ; YR0 = [J1 + 1]','XR0 = [J0 + 1] ; J5 = J1 + 1','R1:0 = DAB Q[J0 += 4]',
                 'XR1:0 = CB L[J2 + 2]','J1:0 = R11:10','R3:0 = J3:0','[J0 + 1] = XR0 ; [K0 + 1] = XR1',
                 'XR3:0 = R7:4 ; [J17 + 2] = XR4','R3:0 = R31:28 ; [J17 + 2] = YR8',
                 'XR3:0 = DAB Q[J0 += 4] ; XR7:4 = DAB Q[K0 += 4]','J0 = [J5 + 1] ; [K6 + 1] = J1',
                 'XR3:0 = Q[J0 += 3]','Q[J2 + 0] = K3:0 ; XR0 = 100','if aeq; do idle']
        for text in valid:
            with self.subTest(valid=text):
                words(text)
        for text in invalid:
            with self.subTest(invalid=text),self.assertRaises(AssemblyError):
                words(text)


class ShiftImmediateRangeTests(unittest.TestCase):
    def test_pair_ranges_follow_output_operand(self):
        # A-9 note 2, 10-171: imm bits byte 4, short 5, normal 6, long 7.
        for dest,src,width in [('R1:0','R3:2',6),('LR1:0','R3:2',7),('SR1:0','R3:2',5),('BR1:0','R3:2',4),
                               ('R0','R1',6),('SR0','R1',5),('BR0','R1',4)]:
            low,high=-(1<<(width-1)),(1<<(width-1))-1
            for value in (low,high):
                with self.subTest(dest=dest,value=value):
                    words(f'X {dest} = LSHIFT {src} BY {value}')
            for value in (low-1,high+1):
                with self.subTest(dest=dest,value=value),self.assertRaisesRegex(AssemblyError,'immediate'):
                    words(f'X {dest} = LSHIFT {src} BY {value}')
        # Dual normal words still use the 7-bit C-9 field (sign in i6).
        # C-9: type 0100, op 001 i6 i5 = 00111, Rn field = imm bits 4:0 = 0, Rm = 2.
        self.assertEqual(words('X R1:0 = LSHIFT R3:2 BY -32'),[0xA6438002])


class ExtensionSlotTests(unittest.TestCase):
    def test_extended_instruction_in_any_slot(self):
        # PR 1-23 Fig 1-6: extension in slot 2, its instruction in any slot.
        self.assertEqual(words('X R0 = R1 + R2; J0 = J0 + 0x10000'),[0x24000022,0x32000100,0x8C002000])
        self.assertEqual(words('X R0 = R1 + R2; K1 = 0x12345678'),[0x24000022,0x72123456,0x99A1D678])
        for text in ['J0 = 0x10000; K0 = 0x10000','X R0 = R1 + R2; J0 = 0x10000; K0 = 0x10000']:
            with self.subTest(text=text),self.assertRaisesRegex(AssemblyError,'one immediate extension|four'):
                words(text)


class StatReadSpellingTests(unittest.TestCase):
    def test_block_prefixed_stat_source(self):
        # A-10/10-195 '{X|Y}Rs = {X|Y}STAT'.
        self.assertEqual(words('XR6 = XSTAT'),words('XR6 = STAT'))
        self.assertEqual(words('YR6 = YSTAT'),[0xAAC49800])
        with self.assertRaises(AssemblyError):
            words('YR6 = XSTAT')
