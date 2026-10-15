import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program


class ControlAddressingTests(unittest.TestCase):
    def test_manual_control_fields(self):
        vectors={
            'IF JEQ; DO J2 = J2 + 5;;':[0x33400000,0xCC022205],
            'IF NJEQ (NF); DO K2 = K31 + 9;;':[0x33008000,0xDC023F09],
            'IF JEQ; DO XR0 = 0x12345678;;':[0x33400000,0x32123456,0xC800D678],
            'ISF0 = NJEQ;;':[0xB3400400],
            'XSF1 += XOR YAEQ;;':[0xB3201980],
            'J3 = NOT J5;;':[0x8F030500],
            'K4 = ROTL K7;;':[0x9FA40700],
            'JB0 = J31 + 5;;':[0x8D003F05],
            'KL3 = K1 - K2;;':[0x9D270104],
            'XR2 = [J31 + 0x40000];;':[0x00023F00,0xB2000400],
            'XR3:0 = Q[J0 + 128];;':[0x00006081,0xB2000000],
            'XR0 = CB [J0 += 1];;':[0x8020A001],
            'YR3:0 = DAB Q[K1 += 4];;':[0x9060E905],
            'JSTAT = XR0;;':[0x88000C3E],
            'XR3:0 = J31:28;;':[0x899C4001],
            'COMP(K3,0x12345678)(U);;':[0x1DA02378,0xF2123456],
            'J0 = [J1 + -129];;':[0x0180217F,0xB2FFFFFF],
            'XR3 = R1 + R2;;':[0xA4000C22],
            'X R3 = R1 + R2; Y R3 = R1 - R2; J1 = J0 + 2; K1 = K0 + 3;;':[0x24000C22,0x28020C22,0x0C012002,0x9C012003],
        }
        for text,wanted in vectors.items():
            with self.subTest(text=text):
                self.assertEqual(assemble(program(text)).sections[0].words,wanted)

    def test_invalid_control_addressing(self):
        for text in ['IF JEQ; DO JUMP start (NP)',
                     'IF JEQ; DO IF JEQ; DO J0 = 1',
                     'IF JEQ; DO J0 = 0x10000; K0 = 0x10000',
                     'ISF0 = AND JEQ','YSF1 += JEQ','ISF2 = TRUE','XSF0 = JEQ',
                     'XR0 = CB [J4 += 1]','XR0 = CB [J0 + 1]',
                     'XR0 = DAB [J0 += 4]','XR3:0 = DAB Q[J0 + 4]',
                     'XR3:0 = L[J0 + 0]',
                     'J1:0 = [J0 += 2]','J2:1 = XR1:0',
                     'JB0 = NOT J0','JL0 = J0 AND J1','JB0 = J0 + J1 (CB)',
                     'XR0 = [J31 + 0x20000]','XR3:0 = Q[J31 + 0x1fffe]',
                     'IF JEQ, JUMP start (NP); ELSE J0 = J1 + J2 (CJMP)']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                assemble(program(text+';;'))


class SequencerSyntaxTests(unittest.TestCase):
    """Manual syntax for prediction, NOP slots and line NF (PR Rev 1.1)."""

    def test_default_prediction_and_line_nf(self):
        vectors={
            # C-39: BP=1 unless (NP); A-14/10-234 'JUMP|CALL <Label> {(NP)}'.
            'JUMP start;;':[0x71080000,0xB3800000],
            'CALL start (ABS);;':[0x70880100,0xB3800000],
            'CJMP (ABS);;':[0xF3084000],
            'IF JEQ, RTI (ABS);;':[0xF3002040],
            # A-14 note 1 / 8-16: all-slot (NF) -> header NF (bit 15);
            # bit 30 of the other slots is CC (C-2).
            'IF JEQ; DO XR0 = R1 + R2 (NF);;':[0x33408000,0xE4000022],
            'IF JEQ; DO XR0 = R1 + R2 (NF); DO J0 = J1 + 1 (NF);;':[0x33408000,0x64000022,0xCC002101],
            'IF JEQ; DO XR0 = R1 + R2 (NF); COMP(J1, J2);;':[0x33408000,0x64000022,0x8D800104],
            'JUMP start (NP); XR0 = R1 + R2 (NF);;':[0x31088000,0x33800000,0xA4000022],
            'XSF0 = XAEQ; XR0 = R1 + R2 (NF);;':[0x33108500,0xA4000022],
            # C-42 'jump <label 1>; nop; nop; nop;;', 8-95 '... (NP); nop;;'.
            'NOP; NOP;;':[0x33C00000,0xB3C00000],
            'JUMP start (NP); NOP; NOP;;':[0x31080000,0x33800000,0x33C00000,0xB3C00000],
            'IF JEQ; DO J0 = J1 + 1; NOP;;':[0x33400000,0x4C002101,0xB3C00000],
            'J0 = J1 + 1; NOP;;':[0x0C002101,0xB3C00000],
        }
        for text,wanted in vectors.items():
            with self.subTest(text=text):
                self.assertEqual(assemble(program(text)).sections[0].words,wanted)

    def test_line_nf_and_prediction_rejections(self):
        for text,message in {
            'IF JEQ; DO XR0 = R1 + R2 (NF); YR0 = R1 + R2':'every slot',
            'IF JEQ; DO XR0 = R1 + R2 (NF); DO J0 = J1 + 1':'every slot',
            'JUMP start; XR0 = R1 + R2 (NF); XR1 = R1 * R2':'every slot',
            'IF JEQ; DO NOP':'conditional body',
            'CJMP_CALL':'requires ABS',       # 10-236: predicted CJMP_CALL must be ABS
            'CALL start (NP)(BP)':'contradictory',
        }.items():
            with self.subTest(text=text),self.assertRaisesRegex(AssemblyError,message):
                assemble(program(text+';;'))

    def test_nf_option_legality(self):
        # NF notes: PR 3-13 (ALU: not PASS/COMP/FCOMP), 4-43 (CLU: not TR/THR/
        # CMCTL transfers), 5-22 (multiplier: not MR4 transfers), 6-19
        # (shifter: not BITEST/BKFPT/STAT loads/TR/THR/CMCTL loads), 7-11
        # (IALU: not COMP); data transfers have no NF in A-13..A-17.
        for text in ['X COMP(R1, R2) (NF)','X SCOMP(R1, R2) (NF)','X FCOMP(R1, R2) (NF)',
                     'X R0 = PASS R1 (NF)','X LR1:0 = PASS R3:2 (NF)','X FR0 = PASS R1 (NF)',
                     'X BITEST R1 BY 3 (NF)','X BKFPT R1:0, R3:2 (NF)',
                     'X STAT = R1 (NF)','X STATL = R1 (NF)',
                     'X TR0 = R0 (NF)','X THR1:0 = R1:0 (NF)','X CMCTL = R0 (NF)',
                     'X R0 = TR0 (NF)','X R0 = CMCTL (NF)',
                     'X R0 = MR4 (NF)',
                     'XR0 = [J0 + 1] (NF)','[J0 + 1] = XR0 (NF)','J0 = J1 (NF)','J0 = 5 (NF)',
                     'COMP(J0, J1) (NF)']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                assemble(program(text+';;'))
        for text in ['X R0 = STAT (NF)','X R0 = ABS R1 (NF)','X R0 = R1 + CI (NF)',
                     'X FR0 = ABS R1 (NF)','X MR1:0 += R0 * R1 (NF)','J0 = J1 + J2 (NF)',
                     'IF JEQ; DO XR0 = R1 + R2 (NF); YR1 = PASS R2; COMP(J0, J1)']:
            with self.subTest(text=text):
                assemble(program(text+';;'))


class ShortBranchTests(unittest.TestCase):
    """(S): toolchain extension selecting the one-slot 15-bit PC-relative
    JUMP/CALL form (PR 1-35 table 1-2; 8-17). Same first word as the long
    form, without the immediate-extension word."""

    def words(self, text):
        return assemble(program(text)).sections[0].words

    def test_short_form_drops_extension(self):
        for long_, short in [('JUMP start (NP)', 'JUMP start (NP)(S)'),
                             ('IF JEQ, JUMP start (NP)(NF)', 'IF JEQ, JUMP start (NP)(NF)(S)'),
                             ('NOP;;\nIF NLC0E, JUMP start (NP)', 'NOP;;\nIF NLC0E, JUMP start (NP)(S)')]:
            with self.subTest(text=short):
                lw, sw = self.words(long_ + ';;'), self.words(short + ';;')
                self.assertEqual(len(sw), len(lw) - 1)
                self.assertEqual(sw[-1] & ~(1 << 31), lw[-2] & ~(1 << 31))

    def test_short_branch_frees_extension_slot(self):
        # A long branch plus an extended IALU op exceeds one extension per
        # packet, so the assembler picks the one-slot form itself (PR 1-23).
        self.assertEqual(self.words('IF NLC0E, JUMP start (NP); K0 = K0 + 1280;;'),
                         self.words('IF NLC0E, JUMP start (NP)(S); K0 = K0 + 1280;;'))
        self.assertEqual(len(self.words('IF NLC0E, JUMP start (NP)(S); K0 = K0 + 1280;;')), 3)

    def test_short_branch_rejections(self):
        far = '.entry start\n.section text, code, 0x100\nstart:\nJUMP far (NP)(S);;\n.section t2, code, 0x8100\nfar:\nNOP;;\n'
        with self.assertRaisesRegex(AssemblyError, 'out of 15-bit range'):
            assemble(far)
        for text, message in {'JUMP start (NP)(S)(ABS)': 'PC-relative', 'CJMP (ABS)(S)': 'applies to JUMP'}.items():
            with self.subTest(text=text), self.assertRaisesRegex(AssemblyError, message):
                self.words(text + ';;')
