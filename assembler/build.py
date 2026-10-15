"""Run Python unit tests and assemble the TS201 corpus into standalone RAM images."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from ts201.assembler import AssemblyError, assemble

ROOT = Path(__file__).resolve().parent

class CountingResult(unittest.TextTestResult):
    subcases = 0
    def addSubTest(self, test, subtest, err):
        self.subcases += 1
        super().addSubTest(test, subtest, err)

def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2, resultclass=CountingResult).run(suite)
    if not result.wasSuccessful():
        return 1
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=build))
    sys.path.insert(0, str(ROOT / "tests"))
    from corpus import condition_corpus, integer_corpus
    from expansion_corpus import expansion_corpus
    from test_appendix_a import REPORT as appendix_report
    examples = [(p.stem, p.read_text(encoding="utf-8"))
                for p in sorted((ROOT / "examples").glob("*.asm"))]
    examples += [("integer_corpus", integer_corpus()[0]),
                 ("condition_corpus", condition_corpus()[0]),
                 ("compute_corpus", expansion_corpus()[0])]
    programs = {}
    try:
        for name, source in examples:
            image = assemble(source, name + ".asm")
            out = run / name
            image.write(out)
            (out / "source.asm").write_text(source, encoding="utf-8")
            programs[name] = dict(words=sum(len(s.words) for s in image.sections),
                                  packets=len(image.packets),
                                  entry_word_address=image.entry_word_address)
        report = dict(passed=True, python=sys.version.split()[0],
                      unit_tests=result.testsRun, subcases=result.subcases,
                      appendix_a=appendix_report, assembled_programs=programs,
                      target_execution_verified=False, hardware_verified=False,
                      vendor_executables_invoked=False, run_directory=run.name)
        for path in (run / "report.json", build / "last-run.json"):
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"PASS: {result.testsRun} unit tests ({result.subcases} subcases), "
              f"{len(programs)} assembled programs")
        print(f"Report: {run / 'report.json'}")
        return 0
    except (AssemblyError, OSError) as error:
        print(error, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
