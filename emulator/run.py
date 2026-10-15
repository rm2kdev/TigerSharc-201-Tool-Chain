"""Run a TS201 RAM image, or assemble and run the included Hello World sample."""
import argparse
from pathlib import Path
import sys
import tempfile

from cpu import Cpu, CpuError, Image

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path, help="image directory or regions.json")
    parser.add_argument("--max-packets", type=int, default=100_000, help="execution limit (default: 100000)")
    args = parser.parse_args()
    try:
        if args.image is None:
            sys.path.insert(0, str(HERE.parent / "assembler"))
            from ts201.assembler import assemble
            with tempfile.TemporaryDirectory(prefix="ts201-hello-") as temporary:
                directory = Path(temporary) / "image"
                assemble((HERE / "hello_world.asm").read_text(encoding="utf-8"), "hello_world.asm").write(directory)
                cpu = Cpu(Image.load(directory)).run(args.max_packets)
        else:
            cpu = Cpu(Image.load(args.image)).run(args.max_packets)
        sys.stdout.buffer.write(cpu.output)
        return 0
    except (CpuError, OSError, ValueError) as error:
        print(f"ts201 emulator: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
