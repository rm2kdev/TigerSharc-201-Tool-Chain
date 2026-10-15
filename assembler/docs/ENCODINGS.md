# Encoding worksheets

The original milestone worksheet below is retained for its literal vectors.
The current support contract is ISA-COVERAGE.md; its larger forms are tested by
`test_compute.py`, `test_encoding_tables.py` and `test_control_addressing.py`.

## Expansion field derivations

For ordinary compute, the word before EX is:

```text
0x20000000 | block<<26 | unit<<24 | type<<20 | op<<15
           | result<<10 | source_m<<5 | source_n | NF<<30
```

Block is 1=X, 2=Y, 3=XY. Unit/type/op are independently entered from C-3..C-12.
Shifter alternate format swaps the two source positions. For example,
`X R3 = R1 + R2` has result=3,m=1,n=2 and all operation selectors zero,
giving 0x24000c22 before EX, or 0xa4000c22 as a standalone packet.
`Y FR3 = R1 * R2` uses unit=3,type=15,op=16, producing 0xabf80c22 with EX.
The worksheets instantiate each row for all three block selectors.

CLU is a separate class: C-2 prints X=2,Y=1,XY=3. Its fields are laid out in
C-6. CLU read/write and scalar/packed shapes must not be inferred
from the ordinary ALU opcode space.

UREG transfers use C-15; the source group's sixth bit occupies bit 7 (the
nearby prose also contains a conflicting bit-15 reference). Wide transfers use
LQ at bit 14 and Q at bit 0. For memory-immediate operands, Q replaces the
low offset bit, so alignment is checked before insertion. Ordinary memory
addresses and offsets are DSP words, including 32-bit immediate extensions.
SDAB-family modifiers instead retain the manual's short-address units.

C-22 conditional execution inverts the source condition for DO, then sets CC
at bit 30 of controlled operations. Any extension stays in slot two and never
receives CC. NF belongs to the condition prefix for the entire packet. Branch
ELSE keeps the branch condition and sets CC on its controlled operations.

C-13/C-14 unary IALU instructions use source_m bits 12:8 as printed.
Static-flag operations follow C-22. Tests check their literal encodings.

No field equation or successful round trip proves instruction semantics,
peripheral timing or legality of every possible packet. See the coverage gaps.

Authority: ADI [ADSP-TS201 Programming Reference, Revision 1.1, April 2005](https://www.analog.com/media/en/dsp-documentation/processor-manuals/34605284816196ts201_pgr.pdf).
Printed pages are used below (PDF page index = printed appendix C page + 781).
These are independent field derivations, not words replayed from firmware or
generated using implementation decoders. Tests keep expected words as literals.

## Field ledger for this subset

| Family | Printed reference | Fields used (inclusive bit ranges) |
|---|---|---|
| Integer ALU | C-26..C-28, fig C-4, tables C-13/C-14 | 29=0, 28=JK, 27:26=3, 25:21=op, 20:16=destination, 13=immediate, 12:8=left; right register 5:1 or signed imm8 7:0 |
| Ureg move | C-29..C-30, fig C-5 | 27:26=2, 25:21=source group, 20:16=source, 13:8=destination group, 5:1=destination |
| Constant | C-31..C-32, fig C-6 | 27:26=2, 25:21=group, 20:16=register, 15=1, 14:0=immediate |
| Scalar load/store | C-32..C-34, figs C-7/C-8 | 27=0, 26=store, 25:21=group, 20:16=data register, 15=post-update, 14=0, 13=immediate, 12:8=base; register 5:1 or signed imm8 7:0 |
| IALU extension | C-37, fig C-9 | 29:24=0x32, 30=JK, 23:0=extension |
| Direct branch | C-38..C-39, fig C-11 | 29:25=0x18, 24=relative, 23=0 (no call), 22=negate, 21:16=condition, 15=0, 14:0=immediate, 30=0 (NP) |
| Branch extension | C-45..C-46, fig C-13 | 29:22=0xCE, 16:0=immediate upper 17 bits |
| NOP / IDLE | C-46..C-47, fig C-14 | 29:22=0xCF, 21:18=0 / 1 |

EX is bit 31 and is set only on the last encoded word. Arithmetic NF is bit 30.
Unused/reserved fields are zero. J/K Ureg group values are 12/13; see PR Appendix B
and the hardware reference Ureg tables. Compare opcodes are 12 (signed) and 13
(unsigned), with unused destination zero. Addition/subtraction are 0/1; OR, AND,
XOR are 18,19,20. Conditions are 0/1/2 for JEQ/JLT/JLE, 4/5/6 for K conditions,
and 8 for TRUE (C-44..C-45).

## Worked vectors

`J3 = J1 + J2;;`:

```text
EX             0x80000000
IALU kind      0x0c000000
destination 3  0x00030000
left 1         0x00000100
right 2 << 1   0x00000004
OR total       0x8c030104
file octets    04 01 03 8c
```

`K3 = K1 - -128;;` is `0x9c232180`: EX, JK=1, integer kind,
subtract opcode, destination 3, base 1, immediate marker, and imm8=0x80.

`J7 = 0x12345678;;`:

```text
word 0: 0x0987d678 = constant kind, J group 12, register 7,
                     immediate marker, imm15 = 0x5678, EX clear
word 1: 0xb2123456 = EX, J extension class,
                     value >> 8 = 0x123456 (C-37 Fig C-10)
reconstruct: (word1 & 0xffffff) << 8 | (word0 & 0xff)
           = 0x12345678; imm15 bits 14:8 equal extension bits 6:0
```

C-37 Figure C-10 (`J7 = 0x2A0` -> extension 0x000002) gives the extension as
bits 31:8 for Ureg loads too. imm15 keeps bits 14:0, so the overlapping
bits agree and any reconstruction (ext<<8 | imm8, ext<<8 | imm15, or
ext[23:7]<<15 | imm15) yields the value. Earlier revisions zeroed extension
bits 6:0, which an ext<<8 | imm8 reading would lose.
K extension sets bit 30. Symbolic constants always reserve the extension
slot, even if their resolved value fits imm15. Literal bit patterns such as
0xFFFFFFFF can use the sign-extended imm15 representation.

`J2 = [J4 + 3];;` is `0x81822403`: EX, J Ureg group, data register 2,
immediate mode, base J4 and offset 3. `[K4 += -1] = K2;;` is `0x95a2a4ff`.
These particular vectors use scalar access: LQ=0. The checker prevents a post-update
load from also writing its own index register.

At word 0x101, `JUMP start (NP);;` with start=0x100 is:

```text
displacement = target_word - packet_start_word = -1 = 0xffffffff
word 0 = 0x31087fff  (relative, TRUE, NP, low 15 bits)
word 1 = 0xb381ffff  (branch extension, upper 17 bits, EX)
```

PC-relative displacement is measured from the first word of the current packet,
not the following packet and not a host byte offset. ABS uses the target word
address itself. Targets must be declared packet starts. Branches are always two
words in this milestone; this sacrifices size to keep symbol placement stable.

## Additional hardening vectors

The literal regressions in `tests/test_hardening.py` include:

| Source | Standalone word(s) | Derivation |
|---|---|---|
| `XYR8 = L[J2 + 2]` | `80886202` | C-17..19: merged group 4, LQ=1, Q=0, J2, imm8=2 |
| `YXR8 = L[J2 + 2]` | `80C86202` | Same fields, merged group 6 reverses X/Y order |
| `XYR3:0 = Q[J0 + 0]` | `81006001` | Broadcast group 8, LQ=1, Q=1 |
| `J31 = J2 + 3` | `8C1F2203` | C-13/14 destination 31 discards value; PR 7-4 |
| `IF NJEQ, RDS` | `B3400040` | C-22 condition JEQ=0 with inversion, RDS bit 6 |
| `XY R11:8 = TR11:8, TR31:16 = XCORRS(R7:4,THR3:0)(CLR)` | `BCD42880` | PR 10-104 example; C-6 transfer type 13, opposite TR half |

For XCORRS, signed CUT is first reduced to a six-bit two's-complement field:
bits 5:4 go to 16:15, bits 3:2 to 6:5, and bits 1:0 to 1:0. The allowed
semantic range is -22..22, not the unsigned six-bit range. Omitting CUT means 0.
Transfer type is 13 (15 with EXT); THL is bit 19, TR source bits 3:2 are at
18:17, and the implied TR source bit 4 equals THL, selecting the opposite half.
RF destination bits 4:2 occupy 14:12; CLR is 11 and CUTR is 10.

`tests/test_registers.py` independently spells group/index/width rows from
Hardware Reference tables 2-13..29. Six-bit source group values split between
word bits 25:21 and 7; destination group is bits 13:8. Peripheral groups cannot
be truncated into the five-bit constant/memory formats.

## Packets and alignment

The checker follows PR 1-38..49 and 7-42..43: maximum four encoded words,
at most one extension in slot two, sequencer first, one J and one K IALU
resource, up to two distinct compute units per block subject to input/output
ports and result-bus restrictions. Transfer write conflicts, post-modify side
effects and selected implicit sequencer/status writes are included. Some legal
transfer read/write dependencies remain conservatively rejected. This is not
automatic scheduling or exhaustive pipeline validation.

DO/ELSE is per operation (PR 10-241..242); unmarked operations are unconditional.
NF for a conditional packet belongs to its header. The RAM guard checks only
active memory operations, without evaluating loop conditions a second time.

Packets can cross quad fetch boundaries (PR 1-18); no four-word packet alignment
is invented. Predicted branches ending in the same quad are rejected. Long/quad
data require their documented alignment, including in the runtime RAM guard.
`.align` uses DSP words and real NOPs for code padding.

Field worksheets and image checks do not constitute physical silicon
validation. See ISA-COVERAGE.md for unresolved manual conflicts and test limits.
