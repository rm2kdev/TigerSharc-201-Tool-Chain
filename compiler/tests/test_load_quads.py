"""External SDRAM loads cannot share a destination quad in a TS201 packet."""
import re
import unittest
from ts201cc import compile_source
from ts201cc.backend import classify, fits
from ts201cc.isel import MI


class LoadQuadTests(unittest.TestCase):
    def load(self, dst, base, size=None):
        return classify(MI('ld', d=[dst], u=[base], imm=0, s=size))

    def test_scalar_loads_in_same_quad_are_separated(self):
        for bank in ('XR', 'YR'):
            for q in range(8):
                for lo in range(4):
                    for hi in range(4):
                        if lo == hi: continue
                        a = self.load(f'{bank}{q*4+lo}', 'J17')
                        b = self.load(f'{bank}{q*4+hi}', 'K2')
                        self.assertFalse(fits([a], b), (a.mi, b.mi))
                        self.assertFalse(fits([b], a), (a.mi, b.mi))

    def test_dual_loads_obey_the_same_rule(self):
        for bank in ('XR', 'YR'):
            for dst in (bank+'0', bank+'1:0'):
                self.assertFalse(fits([self.load(dst, 'J0', 'L')],
                                      self.load(bank+'2', 'K0')))

    def test_disjoint_quads_remain_parallel(self):
        for a, b in (('XR0','XR4'), ('YR0','YR4'), ('XR0','YR0')):
            self.assertTrue(fits([self.load(a,'J0')], self.load(b,'K0')))
        self.assertTrue(fits([self.load('XR3:0','J0','Q')], self.load('XR7:4','K0','Q')))

    def test_internal_register_moves_and_constants_remain_parallel(self):
        a = self.load('XR0', 'J0')
        self.assertTrue(fits([a], classify(MI('mov', d=['XR2'], u=['K0']))))
        self.assertTrue(fits([a], classify(MI('li', d=['XR2'], imm=7))))

    def test_real_optimized_c_has_no_same_quad_load_pairs(self):
        source = '''extern int a[], b[], c[], d[];
        int f(int *p,int *q,int n) {
            int i,s=0; for(i=0;i<n;i++) s+=p[i]*q[i]+a[i]+b[i]+c[i]+d[i];
            return s;
        }'''
        for level in (1, 2):
            _, asm = compile_source(source, optimize=level)
            for packet in asm.split(';;'):
                loads = re.findall(r'\b(XR|YR)(\d+)(?::\d+)? = (?:[LQ] )?\[',packet)
                quads = [(bank,int(reg)//4) for bank,reg in loads]
                self.assertEqual(len(quads),len(set(quads)),packet)


if __name__ == '__main__': unittest.main()
