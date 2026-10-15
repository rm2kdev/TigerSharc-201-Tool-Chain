import unittest
from ts201cc import compile_source, CompileError
from ts201ld import link, LinkError
from cases import CASES, REJECTIONS


class CompilerTests(unittest.TestCase):
    def test_acceptance_corpus(self):
        for name,sources,_,_ in CASES:
            with self.subTest(name=name):
                objs=[compile_source(s,f'{name}{i}.c')[0] for i,s in enumerate(sources)]
                image,_,_=link(objs)
                self.assertIn('_c_main',image.symbols)

    def test_rejections(self):
        for name,source,message in REJECTIONS:
            with self.subTest(name=name):
                with self.assertRaisesRegex((CompileError,LinkError),message): compile_source(source,name+'.c')

    def test_reproducible(self):
        source='int a=42; int main(void){return a;}'
        self.assertEqual(compile_source(source,'x.c'),compile_source(source,'x.c'))

    def test_word_model(self):
        obj,asm=compile_source('unsigned sizes[]={sizeof(int),sizeof(char),sizeof(long),sizeof(int*)}; int main(void){return sizes[0];}')
        self.assertIn('_c_sizes:\n.word 1, 1, 1, 1',asm)

    def test_link_type_mismatch(self):
        a=compile_source('extern int f(int *p); int main(void){return f(0);}','a.c')[0]
        b=compile_source('int f(int p){return p;}','b.c')[0]
        with self.assertRaisesRegex(LinkError,'incompatible declarations'): link([a,b])

    def test_source_coordinates(self):
        with self.assertRaisesRegex(CompileError,'coordinate.c:4:'):
            compile_source('#define X 1\nint main(void) {\n int a=X;\n return missing;\n}', 'coordinate.c')


if __name__=='__main__': unittest.main()
