"""Independent Ureg worksheet from Hardware Reference chapter 2 tables."""
import unittest
from ts201.assembler import AssemblyError
from test_hardening import words


# name, group, index, width, readable, writable. Explicit facts, not an import
# from the encoder registry. SOC bit 5 exercises the split source selector.
ROWS=[
    ('SQCTL',26,24,1,True,True),('SQSTAT',26,27,1,True,False),
    ('RETS',26,4,1,True,True),('FLAGREGST',26,21,1,False,True),
    ('SFREG',26,28,1,True,True),('IVSW',26,16,1,True,True),
    ('WP2CTL',27,2,1,True,True),('WP2STAT',27,6,1,True,False),
    ('TRCB31',10,31,1,True,False),('CCNT1',27,17,1,True,True),
    ('CASTAT10',31,10,1,True,False),
    ('DCS0',32,0,4,True,True),('DCD3',32,28,4,True,True),
    ('DC7',33,12,4,True,True),('DC11',34,12,4,True,True),
    ('DC12',34,24,4,True,True),('DC13',34,28,4,True,True),
    ('DCNT',35,0,1,True,True),('DCNTST',35,4,1,False,True),
    ('DSTAT',35,12,2,True,False),('DSTATCL',35,16,2,True,False),
    # PR Appendix B _LOC addresses (B-22, B-25, B-32).
    ('DSTATL',35,12,1,True,False),('DSTATH',35,13,1,True,False),('DSTATCH',35,17,1,True,False),
    ('IVBUSLOCK',57,18,1,True,True),
    ('SYSCON',36,0,1,True,True),('SDRCON',36,4,1,True,True),
    ('SYSTATCL',36,7,1,True,False),('BMAXC',36,13,1,True,False),
    ('LBUFTX0',37,0,4,False,True),('LBUFRX3',37,28,4,True,False),
    ('LRCTL3',39,3,1,True,True),('LTCTL2',39,6,1,True,True),
    ('LRSTATC1',39,25,1,True,False),('LTSTATC3',39,31,1,True,False),
    ('IVKERNEL',56,0,1,True,True),('IVTIMER0LP',56,2,1,True,True),
    ('IVTIMER1LP',56,3,1,True,True),('IVLINK0',56,6,1,True,True),
    ('IVLINK3',56,9,1,True,True),('IVDMA0',56,14,1,True,True),
    ('IVDMA13',57,6,1,True,True),('IVIRQ3',57,12,1,True,True),
    ('IVHW',57,25,1,True,True),('VIRPT',57,16,1,True,True),
    ('ILATH',58,1,1,True,False),('ILATSTL',58,2,1,False,True),
    ('IMASKH',58,9,1,True,True),('PMASKL',58,6,1,True,False),
    ('INTCTL',58,14,1,True,True),('TIMER1H',58,19,1,True,False),
    ('TMRIN1H',58,23,1,True,True),
]


class RegisterTests(unittest.TestCase):
    def test_move_register_fields_and_direction(self):
        for name,group,index,width,read,write in ROWS:
            rf={1:'XR4',2:'XR5:4',4:'XR7:4'}[width]
            size=(int(width>1)<<14)|int(width==4)
            for dest in (False,True):
                source=f'{name} = {rf}' if dest else f'{rf} = {name}'
                allowed=write if dest else read
                with self.subTest(source=source):
                    if not allowed:
                        with self.assertRaises(AssemblyError): words(source)
                    else:
                        expected=0x88000000|size
                        if dest: expected|=4<<16|group<<8|index<<1
                        else: expected|=(group&31)<<21|index<<16|((group>>5)&1)<<7|4<<1
                        self.assertEqual(words(source),[expected])

    def test_cache_command_sources(self):
        self.assertEqual(words('CACMD0 = J0'),[0x89801E00])
        # PR 8-69 'CADATAi = <Ureg|imm>', '<Ureg> = CADATAi'; B-22 addresses.
        self.assertEqual(words('CADATA0 = J0'),[0x89801E06])
        self.assertEqual(words('J0 = CADATAB'),[0x8BFF0C00])
        self.assertEqual(words('CACMADR10 = J3:2'),[0x89825F10])
        self.assertEqual(words('CACMD0 = 0x4000000'),[0x0BC08000,0xB2040000])
        for source in ['CACMD0 = XR0','CCAIR0 = JSTAT','CACMADR0 = J31:30',
                       'CACMD0 = [J0 + 0]','[J0 + 0] = CASTAT0']:
            with self.subTest(source=source),self.assertRaises(AssemblyError): words(source)

    def test_special_width_and_address_restrictions(self):
        for source in ['DCS0 = XR0','LBUFTX0 = XR1:0','SQCTL = XR1:0',
                       'INTCTL = 1','DCS0 = Q[J0 + 0]','[J0 + 0] = SYSTAT',
                       'WP0CTL = SQCTL','SQCTL = WP0CTL','WP0CTL = WP1CTL',
                       'XR0 = [J31 + 0x1f0080]','SQSTAT = 0','TRCB0 = XR0',
                       'DCS4 = XR3:0','DC0 = XR3:0','LBUFRX4 = XR3:0']:
            with self.subTest(source=source),self.assertRaises(AssemblyError): words(source)
