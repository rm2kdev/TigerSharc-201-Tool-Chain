import argparse
from pathlib import Path
import sys
from .assembler import AssemblyError, assemble


def main():
    parser = argparse.ArgumentParser(description="Independent TS201 subset assembler; output is a RAM image, not flash firmware")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True, help="new output directory (must not exist)")
    args = parser.parse_args()
    try:
        image = assemble(args.source.read_text(encoding="utf-8"), args.source.name)
        image.write(args.output)
    except (AssemblyError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Assembled {sum(len(s.words) for s in image.sections)} words, {len(image.packets)} packets; entry word 0x{image.entry_word_address:08x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
