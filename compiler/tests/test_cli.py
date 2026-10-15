import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
LINKER=ROOT.parent/'linker'
FIXTURES=Path(__file__).resolve().parent/'fixtures/cli'


class CliTests(unittest.TestCase):
    def run_cli(self,script,args,success=True):
        result=subprocess.run([sys.executable,str(script),*map(str,args)],capture_output=True,text=True)
        if success: self.assertEqual(result.returncode,0,result.stderr)
        else: self.assertNotEqual(result.returncode,0,result.stdout)
        return result

    def test_separate_compilation_matches_direct_link(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            sources=[FIXTURES/'main.c',FIXTURES/'fir.c']
            for src in sources: self.run_cli(ROOT/'ts201cc.py',['-c',src,'-o',out/(src.stem+'.tso')])
            self.run_cli(LINKER/'ts201ld.py',[out/'main.tso',out/'fir.tso','-o',out/'separate'])
            self.run_cli(ROOT/'ts201cc.py',[*sources,'-o',out/'direct'])
            for p in (out/'direct').glob('*.bin'):
                self.assertEqual(p.read_bytes(),(out/'separate'/p.name).read_bytes())
            self.run_cli(ROOT/'ts201cc.py',[*sources,'-o',out/'direct'],success=False)

    def test_assembly_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            for name in ('main','value'):
                self.run_cli(LINKER/'ts201ld.py',['-c',LINKER/f'examples/{name}.asm','-o',out/(name+'.tso')])
            self.run_cli(LINKER/'ts201ld.py',[out/'main.tso',out/'value.tso','--entry','entry','--no-startup','-o',out/'image'])
            image=json.loads((out/'image/map.json').read_text())
            self.assertEqual(image['entry_word_address'],image['symbols']['entry'])

    def test_emit_assembly_and_preprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            self.run_cli(ROOT/'ts201cc.py',['-S',FIXTURES/'recursive.c','-o',out/'recursive.asm'])
            self.run_cli(LINKER/'ts201ld.py',['-c',out/'recursive.asm','-o',out/'recursive.tso'])
            self.run_cli(LINKER/'ts201ld.py',[out/'recursive.tso','-o',out/'image'])
            result=self.run_cli(ROOT/'ts201cc.py',['-E',FIXTURES/'main.c'])
            self.assertIn('typedef unsigned int size_t',result.stdout)

    def test_diagnostic_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'bad.c'
            source.write_text('int main(void) { return unknown; }')
            result=self.run_cli(ROOT/'ts201cc.py',['-c',source],success=False)
            self.assertIn('undeclared identifier unknown',result.stderr)
            self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__': unittest.main()
