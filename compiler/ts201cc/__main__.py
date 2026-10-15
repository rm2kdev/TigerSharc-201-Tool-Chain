import argparse
from pathlib import Path
import sys
from .compiler import CompileError, compile_source
from .preprocessor import Preprocessor, PreprocessError
from ts201ld import read_object, write_object, link, LinkError
from ts201ld.linker import write_image


def main():
    p=argparse.ArgumentParser(description='Freestanding TS201 word32 C compiler')
    p.add_argument('inputs',nargs='+')
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('-S',action='store_true',help='emit relocatable assembly')
    mode.add_argument('-c',action='store_true',help='emit relocatable .tso object')
    mode.add_argument('-E',action='store_true',help='preprocess only')
    p.add_argument('-o','--output')
    p.add_argument('-I',dest='includes',action='append',default=[])
    p.add_argument('-D',dest='defines',action='append',default=[])
    p.add_argument('-O',dest='optimize',type=int,choices=(0,1,2),default=1,help='optimization level (default: 1; 0 retains baseline backend)')
    p.add_argument('--entry',default='_c_main')
    for name,default in [('code-base',0x100),('data-base',0x40000),('stack-base',0x140000),('stack-words',4096)]:
        p.add_argument('--'+name,type=lambda x:int(x,0),default=default)
    args=p.parse_args()
    if (args.S or args.c or args.E) and len(args.inputs)!=1: p.error('-S/-c/-E require one input')
    try:
        objects=[]
        for name in args.inputs:
            source=Path(name)
            if source.suffix=='.tso':
                if args.S or args.c or args.E: p.error('compile-only modes require C source')
                objects.append(read_object(source)); continue
            text=source.read_text(encoding='utf-8-sig')
            if args.E:
                text=Preprocessor(args.includes,args.defines).process(text,str(source))
                if args.output: Path(args.output).write_text(text,encoding='utf-8')
                else: print(text,end='')
                return 0
            obj,assembly=compile_source(text,str(source),includes=args.includes,defines=args.defines,optimize=args.optimize)
            if args.S or args.c:
                out=Path(args.output) if args.output else source.with_suffix('.asm' if args.S else '.tso')
                if args.S: out.parent.mkdir(parents=True,exist_ok=True); out.write_text(assembly,encoding='utf-8')
                else: write_object(obj,out)
                return 0
            objects.append(obj)
        write_image(link(objects,entry=args.entry,code_base=args.code_base,data_base=args.data_base,
                         stack_base=args.stack_base,stack_words=args.stack_words),args.output or 'a.out')
        return 0
    except (CompileError,PreprocessError,LinkError,OSError,ValueError) as e:
        print(f'ts201cc: {e}',file=sys.stderr); return 1


if __name__=='__main__': raise SystemExit(main())
