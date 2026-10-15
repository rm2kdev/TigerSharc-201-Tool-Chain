import argparse
import sys
from pathlib import Path
from .linker import LinkError, make_object, write_object, read_object, link, write_image


def main():
    p = argparse.ArgumentParser(description='TS201 word-addressed object linker')
    p.add_argument('inputs', nargs='+')
    p.add_argument('-o', '--output', required=True)
    p.add_argument('-c', '--assemble-object', action='store_true', help='assemble one relocatable .asm to .tso')
    p.add_argument('--entry', default='_c_main')
    p.add_argument('--no-startup', action='store_true')
    p.add_argument('--no-relax', action='store_true', help='keep every branch in its assembled width')
    p.add_argument('--no-fold', action='store_true', help='never copy a branch target packet into its JUMP or CALL line')
    p.add_argument('--gc', action='store_true', help='discard code and data the entry and --keep names do not reach')
    p.add_argument('--keep', action='append', default=[], metavar='NAME', help='exported symbol kept by --gc')
    for name, default in [('code-base',0x100), ('data-base',0x40000), ('stack-base',0x140000), ('stack-words',4096)]:
        p.add_argument('--'+name, type=lambda v:int(v,0), default=default)
    args = p.parse_args()
    try:
        if args.assemble_object:
            if len(args.inputs) != 1:
                p.error('-c requires one input')
            source = Path(args.inputs[0])
            write_object(make_object(source.read_text(encoding='utf-8'), source.name), args.output)
        else:
            result = link([read_object(n) for n in args.inputs], entry=args.entry,
                          startup=not args.no_startup, code_base=args.code_base,
                          data_base=args.data_base, stack_base=args.stack_base, stack_words=args.stack_words,
                          relax_branches=not args.no_relax, fold_branches=not args.no_fold,
                          keep=args.keep if args.gc else None)
            write_image(result, args.output)
        return 0
    except (LinkError, OSError, ValueError) as e:
        print(f'ts201ld: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
