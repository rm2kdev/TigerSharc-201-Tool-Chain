# Independent ADSP-TS201 assembler

An original-source TigerSHARC assembler with explicit DSP word placement and
independent encoding tests. It supports X/Y compute,
packed and floating-point operations, accumulators, merged/broadcast transfers,
named universal registers and explicit parallel/conditional packets.

**Broad ISA coverage with documented restrictions; not certified complete.**
The reproducible Appendix A syntax audit accepts representatives for **261 of
262 entries** (637 of 638 expanded variants). The remaining printed MAC form
conflicts with the encoding table; the chapter's canonical form is supported.
This is syntax acceptance, not an exhaustive encoding or hardware coverage count.
See [hardening and the precise counting method](docs/HARDENING.md).
See [coverage and remaining work](docs/ISA-COVERAGE.md). Encoding and syntax
checks do not establish hardware execution behavior. The sibling `compiler/`
and `linker/` directories provide C compilation and relocatable object linking.
New source is MIT licensed; see LICENSE.

## Reproduce

Requirements: Python 3.10+ only. From `ts201-toolchain`:

```powershell
python assembler/build.py
```

The build runs all assembler unit tests and assembles the checked-in programs
and generated corpora. Each run creates `assembler/build/run-*/`;
`assembler/build/last-run.json` identifies the report. Reports include syntax
counts and assembled program sizes, and explicitly mark target execution as
unverified. No additional SDK, external runtime or network access is needed.

For assembler-only usage, from `assembler/`:

```powershell
python -m ts201 examples/compute_scalar.asm -o build/my-program
python -m unittest discover -s tests -v
```

Choose a new output directory. Results contain little-endian `.bin` sections,
`regions.json`, `map.json` and `symbols.map`. A target loader must implement the
[image interface](docs/DESIGN.md) and any required target initialization.

## Syntax

```text
.entry start
.section text, code, 0x100
start:
    XR1 = 7;;
    XR2 = 9;;
    X R3 = R1 + R2;;
    X R4 = R1 * R2 (I);;
    X FR5 = FLOAT R3;;
    X R6 = LSHIFT R4 BY 2;;
    J0 = result;;
    [J0 + 0] = XR3;;
done: IDLE;;
.section values, data, 0x40000
result: .zero 1
```

`X R3 = R1 + R2` and `XR3 = R1 + R2` select X. `Y` and `XY` select Y or both;
no prefix also means both (PR 2-7), for registers, conditions (`IF ALE`) and
static flags (`SF0 = AEQ`). Operands may repeat the block prefix (`XR0 = FIX XFR1`).
`FR` selects float, `LR` long fixed-point, `SR` packed shorts, and `BR` bytes.
Pairs/quads use aligned ranges such as `R3:2` or `R7:4`; each instruction checks
its operand types/widths. Separate options such as `(I)(U)(C)` and compact
options such as `(UIC)` are accepted. Unsupported option combinations fail.

| Area | Implemented families, with form limitations in the coverage document |
|---|---|
| Integer/packed ALU | Arithmetic, saturation, carry/borrow, ABS, CLIP, MIN/MAX, logic, comparisons, dual add/subtract |
| Float | Arithmetic/multiply, conversions, SCALB, COPYSIGN, MANT, LOGB, RECIPS, RSQRTS, EXTD, SNGL |
| Multiplier | Scalar/paired products, MAC and complex MAC, MR transfers/compaction |
| Shifter | Shifts/rotate, bits, fields, GETBITS/PUTBITS, leading-count/exponent |
| Conversion/CLU | EXPAND/COMPACT/MERGE, SUM/ONES, PR, PERMUTE, trellis/MAX/ACS/DESPREAD/XCORRS forms |
| Transfers | X/Y, XY/YX merged, XY broadcast and J/K scalar/pair/quad; constants; 222 named Uregs; CB/BR and all six DAB variants |
| Sequencer | Conditions, jumps/calls/returns, per-operation DO/ELSE, conditional RDS, static flags, BTB controls, traps, NOP/IDLE |

Unary IALU, CLU selection and static-flag assignments follow the published
encoding tables; accepting an encoding does not establish hardware behavior.

## Packets and memory

One physical line is one packet ending with `;;`; internal semicolons mean
parallel operations. Up to two distinct compute units per block can issue when
their ports, result buses and destinations permit it, alongside up to one J and
one K IALU/transfer operation, within the four-word limit:

```text
X R3 = R1 + R2; Y R3 = R1 - R2; J1 = J0 + 2; K1 = K0 + 3;;
X R3 = R1 + R2; X R6 = R4 * R5;;
IF JEQ; DO X R4 = R1 + R2; K2 = K2 + 1;;
IF NJEQ (NF); DO K2 = K31 + 9;;
IF JEQ, JUMP done (NP); ELSE X R5 = R1 + R3;;
```

Packets hold at most four words. Extensions occupy slot two; EX marks only the
final word. Direct branches use two words and are predicted unless `(NP)`
(`(BP)` is accepted as a spelling of the default); `(ABS)` selects absolute
addressing. Predicted branches ending in the same aligned quad
are rejected. `DO`/`ELSE` applies only to the marked operation; an unmarked
operation is unconditional. On IF/branch lines, `(NF)` goes on every slot that
supports it (PR A-14) or on the header; either sets the line NF bit. `(NF)` is
rejected where the manual excludes it (PASS, COMP, MR4/TR transfers, data
transfers). NOP may fill any slot. Resource,
write, transfer-port and certain special-register conflicts are checked. Some
otherwise legal transfer dependencies are conservatively rejected; this is not
a complete pipeline or peripheral protocol validator.

Section/symbol addresses and ordinary memory offsets are **32-bit DSP words**;
host files use four octets per word. SDAB-family modifiers follow the manual's
**16-bit short-address units** (for example, a circular quad step is 8 shorts).
They are encoded as written; there is no implicit byte conversion. Only the six real internal banks permit image placement.
Gaps, overlaps and bank crossings fail. Memory width follows register ranges;
optional `L`/`Q` must agree with the transfer form. For example, `XYR8 =
L[J0 + 0]` is a merged pair and `XYR3:0 = Q[J0 + 0]` broadcasts a quad.
Literal wide offsets must be aligned. Dynamic pointer alignment and bounds
remain the responsibility of the target program.

J31/K31 are zero arithmetic/address operands; JSTAT/KSTAT name scalar status
transfers. As arithmetic destinations, J31/K31 discard the result while the
operation still updates flags unless NF is set. Post-modify uses the old pointer;
CB/BR/DAB forms require post-modify. See the SDAB unit exception above.

Keywords ignore case; labels are case-sensitive. Directives: `.entry`,
`.section name, code|data, word_address`, `.align`, `.word`, `.zero`. Data directives
cannot inject instructions. Expressions support integers, symbols,
`ADDRESS(symbol)`, unary `+ - ~` and C operators `* / % << >> & | ^ + -`
(division truncates). Ureg immediate loads also take decimal floating
literals (`XR0 = 9.0`, IEEE single). A direct branch drops its extension and
uses the one-slot 15-bit form when the packet has no room for it (or with
`(S)`). Layout sizes/addresses must be literal. Symbolic
instruction operands reserve fixed-size extensions during both passes.

## Further reading

See [coverage](docs/ISA-COVERAGE.md), [encodings](docs/ENCODINGS.md),
[image design](docs/DESIGN.md), [provenance](docs/PROVENANCE.md),
[contributing](CONTRIBUTING.md) and [future C support](docs/C-ROADMAP.md).
