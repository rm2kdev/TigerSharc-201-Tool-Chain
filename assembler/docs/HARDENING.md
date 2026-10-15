# Appendix A audit and assembler hardening

Updated 2026-09-26. This pass used the ADI
[ADSP-TS201 Programming Reference, Rev. 1.1, April 2005](https://www.analog.com/media/en/dsp-documentation/processor-manuals/34605284816196ts201_pgr.pdf)
and [Hardware Reference, chapter 2](https://www.analog.com/media/en/dsp-documentation/processor-manuals/396096833ts201_hwr.pdf).
Page numbers below are printed page numbers. No proprietary implementation or
runtime was incorporated.

## How the instruction count is measured

`tests/appendix_a_cases.json` contains 638 original concrete statements derived
from 262 syntax entries in Appendix A. J and K entries are counted separately,
and the DO/ELSE packet templates are included. Mandatory choices such as `+|-`
are expanded; optional type/flag combinations are not exhaustively expanded.
Each statement records the printed appendix page, listing and a stable audit
entry ID. The numeric ID identifies the original extraction row, not an opcode.

Before this pass, 588/638 representatives assembled, covering 244/262 entries.
Now **637/638 representatives assemble, covering 261/262 entries**. The tests
also instantiate accepted X compute representatives for Y and XY. The complete
build writes these counts and their scope into its `appendix_a` report field.

Counts such as 262 and 317 use different granularity. This audit provides a
repeatable 262-entry denominator; it does not assert a 317-entry correspondence.
Accepting one representative establishes a syntax path, not every encoding,
operand option or instruction's hardware semantics.

The one unresolved printed entry has this shape:

```text
X R9:8 = MR3:2, MR3:0 += R1:0 * R5:4
```

A-7 and 10-133 describe the transfer as selected `MRb`; C-16 and the worked
example on 10-137 instead use `MR3:0`. The supported canonical spelling is:

```text
X R9:8 = MR3:0, MR3:0 += R1:0 * R5:4;;
```

The selected-pair spelling is deliberately rejected. There is no invented pair
selector or silent reinterpretation. Separately, C-15's prose gives a different
quad-MAC `ab` value from C-16; encoding follows C-16. Vendor/hardware evidence is
still needed to corroborate this manual conflict.

## Features added and bugs fixed

| Change | Authority | Checks |
|---|---|---|
| XY/YX merged, XY broadcast/SIMD-copy and cross-J/K transfers | PR 7-17..25, 7-42, C-15..20 | Literal words, field worksheets and `merged_transfer` assembly fixture |
| XDAB/XSDAB/YDAB/YSDAB | PR ch. 7, C-18 | Mode selector, pointer range, width and post-modify tests; execution not claimed |
| XCORRS transfer from the opposite trellis half | PR 10-104 example, C-14 footnote 6 | X/Y/XY, both halves, transfer fields and rejection of same-half operands |
| Signed CUT -22..22 and omitted CUT=0 | PR 10-103 | Boundary values, malformed/oversized operands and independent bit-field expectations |
| Compact options, J31/K31 discard destinations | PR instruction syntax, 7-4 | Separate/combined-option equivalence, duplicate rejection, flag encoding |
| Conditional RDS, explicit ABS for returns, predicted CJMP restriction | PR 10-236..240 | Literal words and negative option cases |
| DO/ELSE applies per operation; unmarked operations execute unconditionally | PR 10-241..242 | Mixed packets, literal CC fields and conditional-memory encoding |
| Same-block compute and parallel transfers; ports/buses/implicit writes | PR 1-38..49 | Valid parallel programs; duplicate unit, destination, port, flag and sequencer-side-effect rejections |
| 222 named Uregs with checked access direction/width | HW ch. 2 tables 2-13..29 | Independent selector rows, reserved holes, SOC-format and cache-source rejection tests |
| Active parallel memory overlap involving a store rejected | PR 1-45..48 | Static absolute-address rejection; dynamic aliasing requires target validation |
| Register-like labels and excessively large instruction operands rejected | Assembler grammar contract | Prevents names becoming unintended register transfers or unhandled integer conversion errors |

The instruction packet checker does not claim to model every silicon pipeline
hazard. It intentionally rejects some transfer read/write dependencies and
unresolved register access shapes. Register definitions retain individual table
references in `ts201/registers.py`; no ADI header was imported.

Image addresses remain 32-bit DSP word addresses. SDAB-family modifiers are the
documented exception: they use 16-bit short-address units, with a circular quad
step of eight shorts (PR 7-29), versus four words for ordinary DAB (7-27). The
assembler preserves the encoded operand; it does not apply host-byte scaling.

## Reproduce and interpret validation

From `ts201-toolchain`:

```powershell
python assembler/build.py
```

Python 3.10+ is the only requirement. The build runs syntax, literal encoding,
packet and image tests, then assembles the source corpora. Its report records
unit-test and subcase totals and the Appendix A audit counts. Generated corpus
expectations remain reference data; the build does not execute their programs.
Passing these checks does not establish physical hardware conformance.
