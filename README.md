# TS201 toolchain

An independent, offline assembler, C compiler and linker for the ADSP-TS201
TigerSHARC DSP. The core tools target the processor instruction set and word
addressing model. Target initialization and image deployment are application
responsibilities.
No ADI tools, SDKs or licenses are used.

| Component | Docs |
|---|---|
| Assembler | [assembler/README.md](assembler/README.md), [CONTRIBUTING](assembler/CONTRIBUTING.md) |
| C compiler | [compiler/README.md](compiler/README.md), [ABI](compiler/ABI.md) |
| Linker | [linker/README.md](linker/README.md), [object format](linker/OBJECT-FORMAT.md) |
| Minimal emulator | [emulator/README.md](emulator/README.md) |

Run the included Hello World program with `python emulator/run.py`. The emulator
executes a small integer subset of TS201 machine code and prints to a simulated
console. It is not a complete processor implementation.

```powershell
tools/check.ps1          # unit tests, including emulator execution tests
tools/check.ps1 --full   # also assemble and link the offline corpora
```

Contributing (including AI agents): read [AGENTS.md](AGENTS.md). Open work is
in [TASKS.md](TASKS.md).

The tools and all checks require Python 3.10+ only. The parser is vendored;
no package installation or external runtime is required. `--full` emits RAM
images for the assembly and C corpora. Those corpus checks validate encoding,
image structure and compilation. The emulator tests execute small programs within
its supported subset; the checks do not certify hardware behavior.
