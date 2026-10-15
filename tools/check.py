"""Run the standalone toolchain checks with Python only.

    python tools/check.py           # assembler, compiler, linker and emulator tests
    python tools/check.py --full    # also assemble and link the offline corpora
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
def step(name, argv, cwd):
    print(f"\n=== {name}\n    {' '.join(str(a) for a in argv)}  (in {cwd.relative_to(REPO)})", flush=True)
    start = time.time()
    code = subprocess.run([str(a) for a in argv], cwd=cwd).returncode
    status = "PASS" if code == 0 else f"FAIL ({code})"
    print(f"=== {name}: {status} in {time.time() - start:.1f}s", flush=True)
    return name, code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="also assemble and link the offline corpora")
    args = ap.parse_args()
    py = sys.executable
    if args.full:
        results = [
            step("assembler tests + corpus images", [py, "build.py"], REPO / "assembler"),
            step("compiler + linker tests + corpus images", [py, "build.py"], REPO / "compiler"),
        ]
    else:
        results = [
            step("assembler unit tests", [py, "-m", "unittest", "discover", "-s", "tests"], REPO / "assembler"),
            step("compiler + linker unit tests", [py, "build.py", "--unit-only"], REPO / "compiler"),
        ]
    results.append(step("emulator execution tests",
                        [py, "-m", "unittest", "discover", "-s", "emulator/tests"], REPO))
    print("\nSummary:")
    for name, code in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    return 0 if all(code == 0 for _, code in results) else 1


if __name__ == "__main__":
    sys.exit(main())
