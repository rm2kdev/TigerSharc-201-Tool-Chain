# Provenance and confidence boundaries

## Sources

Primary ISA authority is ADI's [Programming Reference, Rev. 1.1, April 2005](https://www.analog.com/media/en/dsp-documentation/processor-manuals/34605284816196ts201_pgr.pdf).
Encoding references and literal vector derivations are in ENCODINGS.md. Integer
semantics are in chapter 10, especially compare at 10-207 and transfer at 10-229.
Packet rules are in chapter 1; extension placement is also discussed at 7-42..43.
The [Hardware Reference](https://www.analog.com/media/en/dsp-documentation/processor-manuals/396096833ts201_hwr.pdf),
chapter 2, Figure 2-1, is the memory-map authority; online title metadata differs
from the revision noted on its cover in the workspace handbook. No manual PDF is
redistributed with this source tree.

The assembler, parsers, encoding worksheets, fixtures and example programs
are original source. Selected original disassembly was inspected historically
as secondary corroboration of field usage, not as expected test output. No
proprietary runtime source or executable was copied. An optional vendor-tool
experiment failed its license check and produced no comparison evidence; it
is not part of the build or tests.

## What the standalone checks establish

- Literal vectors derive expected instruction words from published bit fields.
- Syntax audits exercise documented operand and option forms.
- Image checks cover placement, hashes, symbols, packet boundaries and reproducibility.
- Full builds assemble and link corpus images, without executing target code.

The generated reports explicitly mark target execution and hardware validation
as unverified. Dynamic pointer bounds, runtime stack growth, interrupts,
peripherals, boot behavior and timing remain outside these checks.

## Encoding decisions

Unary IALU sources use Jm/Km bits 12:8 (C-13/C-14). Static flags follow C-22.
CLU X/Y selectors follow C-2. COMP flag expectations follow chapter 10-207.

C-25 prints CLU-load source bits 9:7, but its note 1 (standard format, Rm at
9:5) and the 10-198 example `XYTR1:0 = R3:2` agree on a full Rm field; only
natural pair/quad alignment is now required. The CLU selector of C-2 is also
contradicted by C-12's remark that bits 31:26 are identical for all compute
instructions; the explicit C-2 table is kept. A-7/10-133 describes a selected MR pair
transfer alongside quad accumulation, while C-16 and the example on 10-137 use
the full MR3:0 operand. The latter canonical form is supported; the contradictory
pair spelling is rejected. C-15's quad-MAC `ab` prose also conflicts with C-16's
table; the encoder follows C-16. See HARDENING.md.

XCORRS transfers and signed CUT bounds have independent field tests. Named
universal registers retain Hardware Reference table citations. Appendix A
cases are original concrete statements; their acceptance is not proof of every
optional type, flag or packet combination. See HARDENING.md and ISA-COVERAGE.md
for manual conflicts and current test scope.

## Distribution

LICENSE applies to the original source in this directory. Recovered firmware,
vendor manuals, SDK files and proprietary runtimes are not part of the source
package. The standalone tools require Python only.
