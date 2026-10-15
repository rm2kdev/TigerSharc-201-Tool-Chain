"""Run Python compiler/linker tests and emit standalone RAM images for the C corpus."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT.parent / "linker")]
from ts201cc import compile_source
from ts201ld import link
from ts201ld.linker import write_image
from cases import CASES, REJECTIONS

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-only", action="store_true", help="skip corpus image output")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT.parent / "linker/tests"))
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py"),
        unittest.defaultTestLoader.loadTestsFromName("test_linker"),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    if args.unit_only:
        return 0
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=build))
    programs = []
    try:
        for name, sources, _, _ in CASES:
            objs = [compile_source(source, f"{name}{i}.c")[0]
                    for i, source in enumerate(sources)]
            options = (dict(code_base=0x80400, data_base=0xc0200, stack_base=0x100000)
                       if name == "relocated_modules" else {})
            write_image(link(objs, **options), run / name)
            programs.append(name)
        report = dict(passed=True, unit_tests=result.testsRun,
                      compile_rejections=len(REJECTIONS), linked_programs=programs,
                      target_execution_verified=False, hardware_verified=False,
                      python=sys.version, vendor_tools_used=False, run_directory=run.name)
        for path in (run / "report.json", build / "last-run.json"):
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"PASS: {result.testsRun} tests, {len(REJECTIONS)} compiler rejections, "
              f"{len(programs)} linked programs")
        print(f"Report: {run / 'report.json'}")
        return 0
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
