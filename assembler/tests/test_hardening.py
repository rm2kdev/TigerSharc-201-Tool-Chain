"""Manual-derived regressions for previously missing forms and legality bugs."""
import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program


def words(source):
    return assemble(program(source+';;')).sections[0].words


class HardeningTests(unittest.TestCase):
    def test_transfer_literal_vectors(self):
        # PR C-15..19; merge order is illustrated by PR figures 7-7/7-10.
        vectors={
            'XYR8 = L[J2 + 2]':[0x80886202],
            'YXR8 = L[J2 + 2]':[0x80C86202],
            'XYR9:8 = Q[K1 += 4]':[0x9088E105],
            'YXR9:8 = Q[K1 += 4]':[0x90C8E105],
            'XYR3:0 = Q[J0 + 0]':[0x81006001],
            'XYR8 = 7':[0x89088007],
            'J7:4 = XYR9:8':[0x88884C09],
            'XYR9:8 = J7:4':[0x89844411],
            'XYR3:0 = XYR31:28':[0x891C4801],
            'J0 = K0':[0x99A00C00],
            'K0 = [J1 + 0]':[0x81A02100],
            'J31 = J2 + 3':[0x8C1F2203],
            'IF NJEQ, RDS':[0xB3400040],
            'RTI (NP)(ABS)':[0xB3082040],
            'RETI (NP)(ABS)':[0xB3082000],
            'CJMP (BP)(ABS)':[0xF3084000],
        }
        for source,expected in vectors.items():
            with self.subTest(source=source): self.assertEqual(words(source),expected)

    def test_alternate_access_fields(self):
        # C-18 access-group field at 12:10; alternate broadcast group = 9.
        for unit,bank in enumerate(('J','K')):
            for mode,access in [('CB',0),('BR',1),('DAB',2),('SDAB',3),
                                ('XDAB',4),('XSDAB',5),('YDAB',6),('YSDAB',7)]:
                step=8 if 'SDAB' in mode else 4
                source=f'XYR11:8 = {mode} Q[{bank}3 += {step}]'
                expected=0x80000000|(unit<<28)|(9<<21)|(8<<16)|0xE300|(access<<10)|step|1
                with self.subTest(source=source): self.assertEqual(words(source),[expected])

    def test_xcorrs_cut_and_transfer_fields(self):
        # C-6: CLU X selector=2, Y=1; THL selects updated half, while
        # transfer TR bit 4 equals THL (opposite half). CUT is signed six-bit.
        for prefix,selector in [('X',2),('Y',1),('XY',3)]:
            for lower in (False,True):
                half='15:0' if lower else '31:16'
                src='TR27:24' if lower else 'TR11:8'
                for cut in (-22,-1,0,1,22):
                    value=cut&63
                    expected=0xB0000000|(selector<<26)|(13<<20)|(int(lower)<<19)|(2<<17)|(2<<12)
                    expected|=(value>>4)<<15|(1<<7)|((value>>2)&3)<<5|4|(value&3)
                    source=f'{prefix} R11:8 = {src}, TR{half} = XCORRS(R7:4,THR7:4)(CUT {cut})'
                    with self.subTest(source=source): self.assertEqual(words(source),[expected])
        self.assertEqual(words('X TR15:0 = XCORRS(R3:0,THR3:0)'),[0xB8C80000])
        # PR 10-104 example, independently decoded in C-6.
        self.assertEqual(words('XY R11:8 = TR11:8, TR31:16 = XCORRS(R7:4,THR3:0)(CLR)'),[0xBCD42880])

    def test_compact_options(self):
        for body,combined,separate in [
            ('MR3:0 += R1:0 * R3:2','UIC','(U)(I)(C)'),
            ('R5:4 = MR1:0, MR1:0 += R1:0 * R3:2','UIC','(U)(I)(C)'),
            ('R0 = R1 * R2','NUIS','(NU)(I)(S)'),
            ('R0 = COMPACT MR1:0','ISUNF','(I)(S)(U)(NF)'),
            ('R0 = R1 + R2','SUNF','(S)(U)(NF)'),
            ('R0 += FDEP R1 BY R2','SEZF','(SE)(ZF)'),
        ]:
            with self.subTest(body=body): self.assertEqual(words(f'X {body} ({combined})'),words(f'X {body} {separate}'))

    def test_mixed_condition_markers(self):
        self.assertEqual(words('IF JEQ; DO, J2 = J2 + 5; K2 = K2 + 7'),
                         [0x33400000,0x4C022205,0x9C022207])
        self.assertEqual(words('IF JEQ; J2 = J2 + 5; DO K2 = K2 + 7'),
                         [0x33400000,0x0C022205,0xDC022207])
        self.assertEqual(words('IF JEQ; DO J2 = J2 + 5; DO, K2 = K2 + 7'),
                         [0x33400000,0x4C022205,0xDC022207])
        self.assertEqual(words('IF JEQ, JUMP start (NP); ELSE, J2 = J2 + 5; K2 = K2 + 7'),
                         [0x31000000,0x33800000,0x4C022205,0x9C022207])

    def test_legal_parallel_packets(self):
        for source in [
            'X R0 = R1 + R2; X R3 = R4 * R5',
            'XY R0 = R1 + R2; Y R4 = R5 * R6',
            'X R0 = R1 + R2; X R3 = R4 * R5; Y R0 = R1 + R2; Y R3 = R4 * R5',
            'X R0 = R1 + R2; XR4 = [J0 + 0]; YR4 = [K0 + 0]',
            'X R0 = R1 + R2, R8 = R1 - R2; X R4 = R5 * R6',
            'X R3:0 = THR3:0; X R7:4 = MR3:0',
            'XR0 = YR0; XR1 = [K0 + 0]',
            'J0 = [J1 + 0]; K0 = K1 + 1',
            'JUMP start (NP); J0 = J1 + 1',
            'XR0 = 1; XR1 = 2',
            'XR0 = 1; XR1 = [J0 + 0]',
            'XR0 = XR0',
            'J31 = J0 - J1; K31 = K0 - K1',
        ]:
            with self.subTest(source=source):
                result=words(source)
                self.assertLessEqual(len(result),4)
                self.assertEqual([bool(w&0x80000000) for w in result],[False]*(len(result)-1)+[True])
        # Immediate load allocated to K, whose extension selector also changes.
        self.assertEqual(words('XR0 = 0x12345678; J0 = J1 + 1'),[0x1800D678,0x72123456,0x8C002101])

    def test_invalid_shapes_and_options(self):
        for source in [
            'XYR3:0 = L[J0 + 0]','YXR0 = [J0 + 0]',
            'Q[J0 + 0] = XYR3:0','L[J0 + 1] = XYR0',
            'XR0 = XYR0','XYR0 = J0','YXR3:0 = K7:4',
            'XYR1:0 = DAB Q[J0 += 4]','XR3:0 = XDAB Q[J0 += 4]',
            'XYR3:0 = XSDAB Q[J4 += 8]','XYR3:0 = YDAB Q[J0 + 4]',
            'XR0 = BR[J0 + 1]',
            'X TR15:0 = XCORRS(R3:0,THR3:0)(CUT 23)',
            'X TR15:0 = XCORRS(R3:0,THR3:0)(CUT -23)',
            'X TR15:0 = XCORRS(R3:0,THR3:0)(CUT 63)',
            'X TR15:0 = XCORRS(R3:0,THR3:0)(CUT 0)(CUT 1)',
            'X R3:0 = TR3:0, TR15:0 = XCORRS(R3:0,THR3:0)',
            'X R0 = R1 * R2 (UUI)','X MR1:0 += R0 * R1 (ICR)',
            'XSF0 = FLAG0_IN','YSF1 += OR NFLAG3_IN',
            'CJMP_CALL (BP)','CJMP_CALL',
            'IF JEQ; ELSE J0 = 1','DO J0 = 1',
            # PR 10-133 syntax disagrees with its own 10-137 example. Do not
            # invent a selected-pair transfer while updating all four MRs.
            'X R1:0 = MR1:0, MR3:0 += R3:2 * R5:4',
        ]:
            with self.subTest(source=source),self.assertRaises(AssemblyError): words(source)

    def test_parallel_conflicts(self):
        for source in [
            'X R0 = R1 + R2; X R4 = R5 + R6',
            'X R0 = R1 + R2; X R4 = R5 * R6; X R8 = LSHIFT R9 BY 1',
            'X R0 = R1 + R2; X R0 = R3 * R4',
            'X R3:0 = EXPAND SR1:0; X R7:4 = R9:8 * R11:10',
            'X R0 = R1 + R2, R8 = R1 - R2; X R7:4 = MR3:0',
            'X R3:0 = THR3:0; X R8 = LSHIFT R9 BY 1',
            'X R0 += FDEP R1 BY R2; X R4 = R5 + R6',
            'X R1:0 = GETBITS R7:4 BY R9:8; X R10 = R11 + R12',
            'X STAT = R0; X R4 = R5 + R6',
            'X TR3:0 = ACS(TR1:0,TR5:4,R0); X TR3:0 = R7:4',
            'X TR31:16 = XCORRS(R3:0,THR3:0); X TR16 = R4',
            'X R0 = R1 + R2; XR0 = [J0 + 0]',
            'XR0 = [J0 + 0]; XR0 = [K0 + 0]',
            '[J0 + 0] = XR0; [K0 + 0] = XR1',
            'XR0 = XR1; [K0 + 0] = XR2',
            'XYR0 = XYR1; [K0 + 0] = YR2',
            'XR3:0 = DAB Q[J0 += 4]; XR7:4 = DAB Q[K0 += 4]',
            'J0 = [J1 + 0]; [K0 + 0] = J2',
            'K0 = [J0 + 0]; XR0 = 1',
            'J0 = K0; XR0 = 1',
            'JSTAT = K0; J1 = J1 + 1',
            '[J31 + 0] = XR0; YR0 = [K31 + 0]',
            'CALL start (NP); CJMP = XR0',
            'XSF0 = XAEQ; SFREG = J0',
            'IF NLC0E, JUMP start (NP); LC0 = XR0',
            'J0 = J1 + 1 (CJMP); K0 = K1 + 1 (CJMP)',
            'X R0 = R1 + R2; [J0 + 0] = XR0',
        ]:
            with self.subTest(source=source),self.assertRaises(AssemblyError): words(source)

    def test_register_labels_and_oversized_operands(self):
        for name in ('XR0','xyr31','R0','FR1','MR4','THR3','CMCTL','SQCTL','DCNT','SFREG'):
            with self.subTest(name=name),self.assertRaisesRegex(AssemblyError,'register name'):
                assemble(program(f'{name}: NOP;;'))
        for source in ('X TR'+'9'*5000+' = R0',
                       'X TR15:0 = XCORRS(R3:0,THR3:0)(CUT '+'9'*5000+')'):
            with self.subTest(kind=source[:16]),self.assertRaises(AssemblyError): words(source)
