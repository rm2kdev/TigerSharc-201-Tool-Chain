# Independent TS201 linker

Links separately compiled **symbolic `.tso` objects** into word-addressed RAM
images through the sibling TS201 assembler. It implements symbol resolution,
module-private labels, section alignment/placement, relocation expression
resolution, ABI/type compatibility, startup/stack allocation, and link maps.
No ELF/DXE/vendor object compatibility or hardware startup packaging is claimed.

Python 3.10+ and the sibling `assembler/ts201` package are the only dependencies.
The linker does not require the C parser, .NET, an SDK, or internet access.

```powershell
python ts201ld.py ../compiler/build/main.tso ../compiler/build/fir.tso -o build/image
python ts201ld.py -c examples/main.asm -o build/main.tso
python ts201ld.py -c examples/value.asm -o build/value.tso
python ts201ld.py build/main.tso build/value.tso --entry entry --no-startup -o build/assembly-image
```

The `ts201ld.ps1` launcher locates PATH Python or the Codex bundled interpreter.
`TS201_PYTHON` overrides its choice. Create fresh image directories; existing
ones are refused. Object files can be replaced by `-c`.

Default startup calls `_c_main` with a 4096-word stack, then halts. The compiler
[ABI](../compiler/ABI.md) specifies calls and storage. `--no-startup` makes the
exported `--entry` packet the image entry directly and allocates no stack.
`--code-base`, `--data-base`, `--stack-base`, `--stack-words` accept decimal or
hexadecimal values in **DSP words**. Default code/data/stack are at 0x100,
0x40000 and 0x140000. Sections are placed in input order with their required
alignment; the linker never silently spills a section across a bank or gap.

Outputs: binary section files, region manifest `regions.json`, assembler `map.json`
and `symbols.map`, inspectable `linked.asm`, and `link-map.json` with object
digests, public symbols and section placements. File bytes are little endian;
each DSP word occupies four host bytes. Link output is deterministic for the
same inputs/options. The objects' diagnostic basenames are part of their hashes.

## Relocatable assembly

```text
.global entry
.extern value
.section text, code, 4
entry:
    J0 = value;;
    XR0 = [J0 + 0];;
    IDLE;;
```

`.section name, code|data [, alignment]` uses **alignment**, not an address;
the default is four words. `.global` exports a definition and `.extern` imports
one. All other labels are module-private. The assembler's instructions,
`.word`, `.zero`, `.align`, comments and symbol +/- constant expressions are
supported. `.entry` and absolute section placement belong to the final link,
so they are not accepted in objects. A reference must be defined in the same
object or declared `.extern`. Missing exports/imports, duplicate exports,
conflicting ABI/types and malformed objects are errors.

See [OBJECT-FORMAT.md](OBJECT-FORMAT.md) for representation and relocations.

## Link-time optimization

The linker places every packet itself, so it can use final addresses. It
rewrites only the placed assembly (`linked.asm`); the assembler still encodes
every word, and assembling `linked.asm` again gives the same image.

- **Branch relaxation** (default; `--no-relax`, `relax_branches=False`): a
  direct PC-relative JUMP or CALL whose placed target lies within the signed
  15-bit range gets the one-slot form `(S)` instead of an immediate
  extension, one word smaller. Placement iterates to a fixpoint; a branch
  that stops fitting (alignment padding can grow) keeps its extension for
  good. Predicted branches keep the assembler's one-per-aligned-quad rule.
  No packet count changes.
- **Branch-target folding** (default; `--no-fold`, `fold_branches=False`):
  an unconditional JUMP or CALL in compiler output (objects named `*.c`)
  also runs a copy of its target's first packet and continues one packet
  later, saving one executed packet per pass. The copy must be independent
  of the branch line: it reads nothing the line writes (a CALL writes CJMP),
  writes nothing the line writes, writes only compute or J/K registers,
  holds no sequencer operation, and touches no memory the line touches
  unless both use one base register with disjoint literal offsets. The
  line NF bit must suit both. Assembly objects are never folded. Each fold
  costs the copied words; size-bound images may prefer `--no-fold`.
- **Garbage collection** (opt-in: `--gc` with `--keep NAME`, or
  `link(keep=[...])`): code and data that neither the entry nor a kept
  export reaches through symbol references are discarded and listed under
  `discarded` in `link-map.json`. Compiled sections split at labels that no
  packet falls into (code) and at every label (data); surviving compiled data
  keeps its word offset modulo four. Assembly sections stay whole. Anything
  a host tool or test looks up by name must be kept, since references it
  does not see are invisible to the linker.
Linker-owned `_start`, `_halt`, `_stack_low`, `_stack_high` and `_mN_...` names
are reserved. Public names are added to the final map without rewriting C
source or changing the existing assembler.

## Tests

From this directory: `python -m unittest discover -s tests -v`.
The compiler's `python build.py` runs compiler/linker unit tests and emits corpus
images, including calls and data pointers across modules and alternate bank
placements. These Python-only checks do not execute target code or certify
real hardware behavior.

Original linker code is MIT licensed; see LICENSE.
