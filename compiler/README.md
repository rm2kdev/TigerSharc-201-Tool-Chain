# Independent TS201 C compiler

A working freestanding **word32 integer/pointer C subset**, with separate
compilation and an independent stack ABI. It emits TigerSHARC assembly and
relocatable `.tso` objects, and drives the sibling assembler and linker to make
standalone RAM images with explicit word-addressed region manifests.

This is an initial compiler, **not a complete ISO C implementation, an ADI ABI
replacement, or a production firmware compiler**. Floating point, 64-bit types,
automatic X/Y vectorization, ELF interoperability, standard libc and hardware boot/flash
packaging are not implemented. Unsupported constructs have diagnostics; the
test corpus is evidence of tested behavior, not a conformance certification.

## Run

Python 3.10+ is required. The parser is vendored, so compilation and linking
need no package installation, network, ADI SDK, license manager or vendor
executables. Keep `compiler`, `linker`, and `assembler` as sibling directories.

From this directory:

```powershell
python ts201cc.py tests/fixtures/cli/main.c tests/fixtures/cli/fir.c -o build/fir-image
python ts201cc.py -S tests/fixtures/cli/recursive.c -o build/recursive.asm
python ts201cc.py -c tests/fixtures/cli/main.c -o build/main.tso
python ts201cc.py -c tests/fixtures/cli/fir.c -o build/fir.tso
python ../linker/ts201ld.py build/main.tso build/fir.tso -o build/separate-image
python ts201cc.py -E tests/fixtures/cli/main.c
```

The included `ts201cc.ps1` also locates Python in the Codex bundled runtime if
Python is absent from PATH. `TS201_PYTHON` can specify a particular interpreter.

Use fresh output directories for linked images. Existing image directories
are deliberately rejected. Individual `-c`/`-S` output files are replaced.
The image directory contains `.bin` sections, `regions.json`, `map.json`,
`symbols.map`, `linked.asm`, and `link-map.json`. A target loader must implement
the region manifest format. The included test fixture returns 70 and writes 70 to the
exported `_c_result` symbol when executed on a compatible target. These are RAM
images, not flashable firmware.

`-I directory` and `-D NAME=value` are repeatable. `--entry _c_name`,
`--code-base`, `--data-base`, `--stack-base`, and `--stack-words` customize
linking; numeric options accept `0x` hexadecimal. All addresses and sizes are
**DSP words**. The startup entry must be `int name(void)`.

## Implemented language profile

- Signed/unsigned `char`, `short`, `int`, `long`, enums and `_Bool`;
  all integer objects occupy one 32-bit DSP word.
- Pointers, address/dereference, multidimensional arrays, indexing, pointer
  arithmetic/difference, structs with arrays, member access and struct copy.
- Globals, `extern`, internal `static` linkage, static locals, automatic
  variables, file-scope typedefs, scalar/array/nested-brace struct initializers,
  zero initialization, string literals, global symbol-plus-offset pointers.
- Arithmetic including signed/unsigned division and remainder, bitwise and
  variable shifts, comparisons, assignments, compound assignments, increment,
  casts, `sizeof`, comma, conditional and short-circuit expressions.
- Blocks, `if`, `while`, `do`, `for`, `switch`/fallthrough, `break`, `continue`,
  `goto`, returns, prototypes, scalar/pointer arguments and return values,
  nested calls, recursion and mutual recursion.
- Volatile accesses are emitted in order and never removed; other memory
  accesses may be reordered, merged or removed only when alias analysis proves
  it safe. This does not implement Ureg/MMIO primitives.
- Includes, object/function macros, `#undef`, conditional directives and
  `#error`; guarded profile headers for `stdint.h`, `stddef.h`, `stdbool.h`,
  `limits.h`. Function macro replacements are rescanned. Preprocessing is a
  restricted implementation, not a full conforming C preprocessor.

See [ABI.md](ABI.md) for the precise data and call model.

## Explicit limits

The default language profile has no floating types, `long long`, union/bitfield/atomic types, function pointers,
variadics, K&R or unprototyped declarations, aggregate parameters/returns,
variable-length or flexible arrays, designated initializers, implicit brace
elision for nested aggregates, block-scope typedefs, shadowed struct tags,
explicit alignment, inline assembly, dynamic allocation, or libc. The explicitly
supported DSP intrinsic is documented below. The existing opt-in `extended=True`
profile remains available for additional legacy C constructs.
No macro stringification/token pasting/variadic macros, implementation pragmas,
trigraph/digraph processing, or full `intmax_t` preprocessing arithmetic.
The compiler examines file-scope declarations before emitting functions; it
does not enforce every ISO declaration-order constraint. Signedness and
representation use the documented profile rather than the host Python model.

Signed overflow, invalid shifts, out-of-bounds pointers, unsequenced side
effects and uninitialized reads retain C's undefined-behavior restrictions;
this compiler is not a source-level undefined-behavior detector. Runtime divide
by zero terminates at an `IDLE` packet instead of hanging. Signed
`INT_MIN / -1` is outside the supported defined-input contract. Stack capacity
is finite; callers must provision enough space. There is no runtime stack
overflow handler. Tests check both stack guard regions and stack restoration.

## Optimization

`-O1` is the default. `-O0` retains the original spill-based backend for
comparison/debugging and as the reference in differential tests. `-O1` and
`-O2` use the IR backend described below; `-O2` adds inlining, unrolling and
software pipelining. Examples: `python ts201cc.py -O2 -S example.c -o example.asm`
or `compile_source(source, optimize=2)` / `Compiler(name, extended=True, optimize=2)`.

Pipeline (`ts201cc/lower.py` -> `opt.py` -> `loops.py` -> `isel.py` ->
`regalloc.py` -> `backend.py`):

- **IR and SSA.** Functions lower to a three-address IR with basic blocks;
  conditions lower straight to branches and loops are emitted rotated (guard
  plus bottom test). Non-address-taken, non-volatile scalars become SSA values.
- **Scalar optimization.** Sparse conditional constant propagation,
  peephole simplification, dominator-scoped value numbering with load/store
  forwarding along extended basic blocks, dead code elimination, CFG
  simplification, loop-invariant code motion (including loads proven
  unaliased), strength reduction of induction-variable products and shifts,
  and constant division by magic multiplication (unsigned and signed).
- **Alias analysis.** Distinct objects, constant offsets from one base,
  non-escaping stack objects and access widths are distinguished. Scalar
  globals whose address never escapes are unaliased: `static` objects are
  decided per unit, external ones when the build passes the whole-program
  `address_taken` option (names whose address is taken anywhere).
- **If-conversion.** Clamp triangles become `MIN`/`MAX`; small side-effect-free
  triangles and diamonds inside loops become conditional moves
  (`IF cond; DO ...`), so loop bodies become single blocks.
- **Loops.** Counted innermost loops without calls use the hardware loop
  counters (`LC0`, or `LC1` when the body divides): the exit test becomes one
  decrement-and-branch. `-O2` fully unrolls small constant trip counts, unrolls
  other small single-block loops by 4 (with an exact remainder loop), and
  software-pipelines them in two stages (address/arithmetic work and
  alias-free loads of iteration i+1 overlap the stores of iteration i).
- **Inlining (`-O2`).** Same-unit functions are optimized bottom-up and cloned
  into callers: small callees, single-use statics, and larger callees at call
  sites inside loops. Unreferenced static functions are dropped.
- **Builtins.** When only declared, `memcpy`/`memset` expand to IR loops (a
  quad-word path first aligns the destination, then moves four words per
  transfer: `Q` loads when the source is aligned too, otherwise data-alignment-
  buffer `DAB Q` loads, two packets per eight words; option `no_dab` disables
  DAB, e.g. for code that runs inside interrupt handlers, since the DAB state
  is not preserved across an interrupt) and `toupper`/`tolower` expand to a conditional move.
  Calls to `exit`/`abort`/`_Exit` and `_Noreturn` functions (plus names in the
  `noreturn` option) end their block, so a function that only calls them
  stays a leaf.
- **Register classes.** Every value lives in an X compute register or in a J
  or K IALU register. A min-cut chooses compute versus IALU placement from the
  operations that use each value; IALU values are split between J and K to
  balance the two IALUs (each issues one operation, including memory
  accesses, per packet). Shifter/ALU work is rebalanced in hot blocks.
- **Allocation.** Graph colouring with conservative coalescing, register
  pairs/quads, spilling and rematerialization. Loop values rotate through a
  small register pool; block-local values are renamed after allocation to
  remove false dependences. Only used callee-saved registers are saved, with
  pair/quad transfers.
- **Scheduling.** Pre- and post-allocation list scheduling packs up to four
  instruction words per packet under the assembler's resource rules (J/K
  IALUs, compute units, register-file ports, extension words, sequencer slot),
  with memory dependences from the alias analysis. Compares feeding a branch
  are fused into the flag-setting arithmetic that produced the value.
  Post-modify addressing (`[J0 += k]`), indexed addressing (`[J0 + J1]`) and
  cold-path block layout remove further instructions.
- **Division by a variable** uses an inline macro that normalises with `LD0`
  and retires one quotient bit per two packets (hardware loop), instead of a
  fixed 32-step loop. Division by zero still stops at `IDLE`.

Ordinary loads and stores keep their order unless alias analysis proves them
independent; volatile accesses are never removed, merged or reordered.

`int __builtin_ts201_mul_q16(int a, int b)` is available at every optimization
level without a declaration. It returns the low 32 bits of the signed 64-bit
product arithmetically shifted right by 16, including negative rounding. Its
arithmetic lowers to a native wide multiply and long shift, without a helper
call. This does not add general 64-bit C types.

Debugging aids: `TS201_DISABLE=pass,...` switches off individual passes
(`sccp`, `instcombine`, `gvn`, `dce`, `cfg`, `licm`, `legalize`, `sr`,
`ifconv`, `inline`, `unroll`, `balance`, `flags`, `prera`, `rename`, `sched`,
`postinc`, `mcleanup`, `unrollm` (full unroll of multi-block loops), `thread` (phi-constant branch threading), `areassoc` (loop address reassociation), `fext` (bit-field extract), `short` (one-slot `(S)` intra-function branches)), and `TS201_NOOPT_FUNCS=f,g` compiles the named
functions with the `-O0` generator inside an optimized unit.

## Verify

```powershell
python build.py
python build.py --unit-only
```

Python 3.10+ is the only requirement. Both commands run all compiler and linker
unit tests, including frontend acceptance/rejection, source diagnostics,
cross-module types, optimizer and packet checks, and image reproducibility.
The default command also compiles and links the C corpus, including alternate
code/data/stack bank placements. It creates `build/run-*/report.json` and updates
`build/last-run.json`. `--unit-only` skips corpus image output.

These checks do not execute target programs or validate their runtime register,
memory, stack or timing behavior. Reports explicitly record that boundary.

## Source and provenance

`ts201cc/compiler.py` contains type/layout/ABI logic and the `-O0` code
generator; the optimizing backend is `lower.py`, `ir.py`, `ssa.py`, `opt.py`,
`loops.py`, `inline.py`, `isel.py`, `regalloc.py` and `backend.py`.
`preprocessor.py` implements the restricted preprocessor. Original toolchain
source is MIT licensed. `vendor/pycparser` is pycparser 3.00, BSD licensed; see
[vendor/README.md](vendor/README.md) and its retained license. It supplies syntax
trees only and is not a TS201 compiler or runtime.

Processor instruction encodings remain owned by the existing assembler.
The supplied *ADSP-TS201 TigerSHARC Processor Programming Reference* informs
word addressing, register use, interlocks and computed calls; its contents are
reference material, not build instructions. See [ABI.md](ABI.md).
