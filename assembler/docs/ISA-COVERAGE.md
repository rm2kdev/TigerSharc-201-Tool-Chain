# ISA coverage, 2026-09-26

Broad encoding support with explicit restrictions; **not a complete or
hardware-certified assembler**. The repeatable Appendix A syntax audit accepts
representatives for **261/262 entries**, or **637/638 mandatory-choice variants**.
The remaining printed MAC spelling conflicts with the chapter example and
encoding table; the canonical form is already supported. These counts do not
measure exhaustive operand, option, packet or semantic coverage. See
[the counting method and conflict](HARDENING.md).

## Implemented families

Authority: ADI [Programming Reference Rev. 1.1](https://www.analog.com/media/en/dsp-documentation/processor-manuals/34605284816196ts201_pgr.pdf),
Appendix C, chapter 10 semantics and chapter 1 packet rules. Named Uregs use
[Hardware Reference](https://www.analog.com/media/en/dsp-documentation/processor-manuals/396096833ts201_hwr.pdf)
chapter 2.

| Tables | Encoding support | Static checks / limit |
|---|---|---|
| C-3 fixed ALU | Scalar, dual, long, short/byte arithmetic, comparison, saturation, logic/carry, dual add/subtract | Literal fields and scalar corpus assembly; packed/long semantic coverage incomplete |
| C-4 conversion | EXPAND, COMPACT, MERGE, SUM, ONES, PR, VMIN/VMAX, BFOINC, PERMUTE | Independent field rows; incomplete semantic coverage |
| C-5 float | 32-/40-bit arithmetic, conversions, estimates, min/max, compare, dual add/subtract | Field and syntax checks; floating-point execution unverified |
| C-6 CLU | TR/THR/CMCTL reads, MAX/TMAX, ACS, DESPREAD and XCORRS with/without transfers; signed/default CUT | Independent field tests; block selectors follow C-2 |
| C-7 multiplier | Scalar/dual/wide products, real/complex/conjugate MAC, old-result transfer, MR loads/extraction/compaction | Literal product/MR fields; quad-MAC manual wording conflict remains |
| C-8..12 shifter | Scalar/long/packed shifts, rotate, bits, fields, bitstream, leading-count/exponent, status/BFOTMP, CLU writes | Field tests and corpus assembly |
| C-13..14 IALU | All defined arithmetic/logical opcode numbers, base/length results, extended compares, J31/K31 discard destinations | Literal words; unary source follows C-13/C-14 |
| C-15..20 transfers | X/Y, XY/YX merged, XY broadcast, J/K including cross-IALU; scalar/pair/quad; constants; extended offsets; CB/BR/DAB/SDAB/XDAB/XSDAB/YDAB/YSDAB | Field, alignment and width checks; dynamic addresses unverified |
| C-21..26 sequencer | Conditions, direct/computed calls/jumps, returns, per-operation DO/ELSE, conditional RDS, static flags | Condition and extension fields; static flags follow C-22 |
| C-27..28 miscellaneous | NOP, IDLE, BTBINV/BTBEN/BTBDIS/BTBLOCK/BTBELOCK, TRAP, EMUTRAP | Literal encodings; no interrupt/debug/timing conformance claim |
| HW ch. 2 Uregs | 222 names: index/base/length/status, sequencer, cache, interrupt/timer, DMA, link, system and debug | Table-derived selectors/widths/access checks; peripheral protocols unverified |
| PR 1-38..49 packets | Same-block distinct units, two IALUs/transfers, input/output ports, result buses, write conflicts, selected implicit effects | Positive compute/transfer programs and negative conflicts; conservative transfer dependencies remain |

Compact options such as `(UIC)`, `(ISU)` and `(SEZF)` are accepted as well as
separate options. Line NF on IF/branch lines comes from every-slot (NF) or the header. Post-modify, register range,
width and read/write checks reject unsupported shapes with a source-line error.

## Evidence

The standalone workflow runs Python syntax, literal encoding, packet and image
tests, then assembles the example and generated corpora. Reference register and
memory expectations are not executed by this workflow. Reports record actual
test counts and explicitly mark target execution as unverified. Test-case and
subcase totals must not be presented as opcode counts.

## Remaining limits

1. Exhaustive combinations of operand types, options, flags and packet effects
   are not proven. More packed/long, 40-bit float, rounding/saturation, CLU,
   accumulator and exceptional-float semantic tests are needed.
2. A-7/10-133's MR-pair transfer with quad accumulation conflicts with C-16 and
   10-137. The canonical MR3:0 form is supported. C-15's `ab` prose conflicts
   with C-16's table; the latter is followed. CLU-load sources use the full
   standard Rm field (C-25 note 1, 10-198), naturally aligned.
3. Unary IALU source selection, CLU block selection and static flags follow the
   published encoding tables. Independent hardware confirmation is unavailable.
4. Packet validation is not an exhaustive pipeline or peripheral validator.
   Some legal old-value transfer dependencies are rejected. Dynamic aliases,
   pointer alignment and runtime bounds are not statically solved.
5. Register aliases, JTAG-only interfaces and host-only AutoDMA interfaces are
   not exposed. Some documented peripheral access widths conflict between tables
   and prose; the registry deliberately offers a conservative subset. Read/write
   checks do not validate the timing/protocol of using a peripheral.
6. Independent disassembly and optional vendor/hardware vectors would add
   confidence. The earlier optional vendor probe failed its license check and
   supplied no comparison data. It remains outside the build/test suite.

The sibling compiler and linker provide C and relocatable module support.
Application-specific memory placement, image deployment and physical hardware
verification remain separate responsibilities.
