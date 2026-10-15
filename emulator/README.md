# Minimal TS201 emulator

A Python-only functional emulator for the integer instructions needed by the
included console Hello World. This is a small starting point, not a complete or
cycle-accurate ADSP-TS201 implementation. Python 3.10+ is sufficient; no packages
or vendor tools are required.

## Run Hello World

From the toolchain root:

```powershell
python emulator/run.py
```

Output:

```text
Hello, world!
```

The runner assembles `hello_world.asm` into a temporary RAM image using the
existing assembler, loads its binary words, and executes until `IDLE`. It leaves
no build files behind. The program reads a zero-terminated string from RAM and
writes one character at a time. Changing the string changes the output.

To assemble an image explicitly and run it later (the output directory must not
already exist):

```powershell
Push-Location assembler
python -m ts201 ../emulator/hello_world.asm -o ../emulator/build/hello-world
Pop-Location
python emulator/run.py emulator/build/hello-world
```

The image argument can also name `regions.json`. The loader requires the adjacent
`map.json` and region binaries, checks lengths and hashes, and obtains instruction
packet boundaries from the binary EX bits. Source text, symbols and disassembly
metadata are not executed. Output goes to stdout; errors go to stderr with exit
code 1. `--max-packets 100000` controls the execution limit.

## Supported subset

- J0..J30 and K0..K30 scalar registers; J31/K31 zero operands and discarded
  arithmetic results.
- Short and extended constant loads, integer ADD/SUB and signed/unsigned COMP.
  Only the J/K zero flags are modeled; ADD/SUB support NF.
- Scalar J/K memory loads and stores with immediate or register offsets and
  optional post-modification.
- Direct relative/absolute JUMP, unconditional or using JEQ/KEQ and their
  inversions, with short or extended targets. Prediction has no timing effect.
- NOP and IDLE. IDLE ends the run because there are no interrupt sources.

Each packet contains one operation and, when needed, its immediate extension.
Parallel operations, compute X/Y instructions, floating point, other conditions
and status flags, CALL/return, interrupts, DMA, peripherals and self-modifying
code are unsupported. Unsupported instructions raise an error rather than being
silently skipped. General compiler output will need additional instructions.

Addresses count 32-bit words. The six internal RAM banks are available at
`0x000000`, `0x040000`, `0x080000`, `0x0C0000`, `0x100000` and `0x140000`, each
containing `0x20000` words. Unloaded RAM and registers start at zero; zero flags
start clear. This is a deterministic test setup, not a hardware reset model.

A store to the synthetic console address **`0xFFFF0000`** appends its low byte to
the output. This address is a host convention, not a physical TS201 peripheral.
Console reads and other unmapped addresses fail. Program bytes are printed when
execution finishes successfully.

## Checks and references

```powershell
python -m unittest discover -s emulator/tests -v
python tools/check.py
```

Tests cover literal machine-word vectors, arithmetic, loads/stores, branches,
Hello World execution, changed message data, image validation and CLI failures.
The normal toolchain gate includes them. The assembly/C corpus build checks do
not imply that all corpus images can execute in this emulator.

Instruction fields follow the existing [encoding worksheets](../assembler/docs/ENCODINGS.md)
and the [ADSP-TS201 Programming Reference](https://www.analog.com/media/en/dsp-documentation/processor-manuals/34605284816196ts201_pgr.pdf),
especially Appendix C tables C-13/C-14, C-16/C-17 and C-21.

License: [MIT](LICENSE).
