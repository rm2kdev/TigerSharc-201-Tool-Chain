import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program


class ComputeTests(unittest.TestCase):
    # Independent literal field calculations, PR C-2..12/C-16/C-20/C-24.
    VECTORS = {
        'X R3 = R1 + R2;;': [0xA4000C22],
        'Y R3 = R1 - R2 (S);;': [0xA8028C22],
        'XY SR3 = R1 + R2;;': [0xAC600C22],
        'X LR3:2 = R5:4 + R7:6;;': [0xA4300887],
        'X FR3 = R1 + R2;;': [0xA5400C22],
        'Y FR3 = R1 * R2;;': [0xABF80C22],
        'X R3 = R1 * R2 (I);;': [0xA7040C22],
        'X R3 = LSHIFT R1 BY 3;;': [0xA6020C61],
        'X R3 = ASHIFT R1 BY -1;;': [0xA6068FE1],
        'X R3 = BSET R1 BY 31;;': [0xA6B60FE1],
        'X R3 = LD0 R1;;': [0xA6C00C20],
        'XR3 = J2;;': [0x89820006],
        'XR1 = 5;;': [0x88018005],
        'XR2 = [J0 + 0];;': [0x80022000],
        'CJMP (NP)(ABS);;': [0xB3084000],
        'RTI (NP);;': [0xB3082040],
        'RETI (NP);;': [0xB3082000],
        'CALL start (NP);;': [0x31880000,0xB3800000],
        'IF XAEQ, JUMP start (NP);;': [0x31100000,0xB3800000],
        'BTBEN;;': [0xB3C80002],
        'TRAP(31);;': [0xB3D0001F],
        'EMUTRAP;;': [0xB3E00000],
    }

    def test_manual_vectors(self):
        for text,wanted in self.VECTORS.items():
            with self.subTest(text=text):
                self.assertEqual(assemble(program(text)).sections[0].words,wanted)

    def test_negative_operands(self):
        for text in ['X R32 = R1 + R2','X R2:1 = R3:2 + R5:4','X R3 = R1:0 + R2',
                     'X R0 = R1 * R2 (I)(T)','X R1:0 = R2 * R3 (T)',
                     'X FR0 = R1 AND R2','X R0 = LSHIFT R1 BY 32',
                     'X R0 = LSHIFT R1 BY -33','X R0 = BSET R1 BY -1',
                     'X R0 = BSET R1 BY 32','X BR0 = ROT R1 BY 1',
                     'X R0 = R1 + R2 (BOGUS)','X R0 = R1 + R2 (S)(S)',
                     'XR3:0 = [J0 + 2]','XR1:0 = [J0 + 1]',
                     'XR2:1 = YR3:2','XR1:0 = YR0','XR1:0 = 5',
                     'TRAP(32)','TRAP(-1)','IF XBAD, JUMP start (NP)',
                     'JUMP start (BP)(NP)']:
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                assemble(program(text+';;'))

    def test_separate_compute_blocks(self):
        image=assemble(program('X R3 = R1 + R2; Y R3 = R1 + R2;;'))
        self.assertEqual(image.sections[0].words,[0x24000C22,0xA8000C22])
        for text in ('X R0 = R1 + R2; X R4 = R5 + R6',
                     'X R0 = R1 + R2; X R0 = R5 * R6'):
            with self.subTest(text=text),self.assertRaises(AssemblyError):
                assemble(program(text+';;'))

    def test_predicted_branch_quad(self):
        with self.assertRaisesRegex(AssemblyError,'same aligned quad'):
            assemble(program('JUMP start (BP);;\nJUMP start (BP);;'))
        assemble(program('JUMP start (BP);;\n.align 4\nJUMP start (BP);;'))

    def test_immediate_shift_boundaries(self):
        for block in ('X','Y','XY'):
            for dest,src,width in [('R0','R1',6),('LR1:0','R3:2',7),('SR0','R1',5),('BR0','R1',4)]:
                for value in (-(1<<(width-1)),-1,0,(1<<(width-1))-1):
                    with self.subTest(block=block,dest=dest,value=value):
                        words=assemble(program(f'{block} {dest} = ASHIFT {src} BY {value};;')).sections[0].words
                        encoded=((words[0]>>5)&31)|(((words[0]>>15)&(3 if width==7 else 1 if width==6 else 0))<<5)
                        self.assertEqual(encoded & ((1<<width)-1),value & ((1<<width)-1))
