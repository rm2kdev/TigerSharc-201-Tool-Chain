# Opcode audit against PR Rev 1.1

Assembler vs *ADSP-TS201 Programming Reference* Rev 1.1 (`TS201 Reference.pdf`,
PDF page = printed C-page + 782, A-page + 722). Status values:

- **verified**: every row's fields compared bit-for-bit with the encoder.
- **fixed**: discrepancy found and fixed (commit in the log below).
- **open**: discrepancy found, not yet fixed (see Open items).
- **manual-conflict**: manual contradicts itself; chosen reading noted.

## Encodings (Appendix C)

| Group | Manual | Status | Notes |
|---|---|---|---|
| Instruction structure, ITYPE, EX, CC/NF | C-1..C-2, Table C-1 | verified | |
| Compute standard/alternate formats | C-3..C-4, C-19, Figs C-2/C-3 | verified | |
| ALU fixed arithmetic/logic | C-5..C-7, Table C-3 | verified | all 27 rows + footnotes 1..8 |
| ALU fixed data conversion | C-7..C-10, Table C-4 | verified | EXPAND/COMPACT/MERGE/SUM/ONES/PR/BFOINC/PERMUTE |
| ALU float | C-10..C-12, Table C-5 | verified | |
| CLU | C-12..C-14, Table C-6 | verified / manual-conflict | block selector 1101=Y,1110=X (C-2 table) vs C-12 "bits 31:26 identical" prose; C-2 kept |
| Multiplier | C-15..C-18, Table C-7 | verified / manual-conflict | quad MAC `ab` prose (C-15) vs table (C-16): table followed |
| Shifter word/long/short/byte/single-operand | C-19..C-24, Tables C-8..C-11 | verified | |
| CLU register load | C-25, Table C-12 | fixed / manual-conflict | Rm at 9:7 (text) vs standard 9:5 (note 1, 10-198): full field, natural alignment |
| IALU integer | C-26..C-29, Tables C-13/C-14 | verified | unary source at Jm (12:8) |
| IALU move | C-29..C-30, Table C-15 | verified | |
| IALU load data (imm15) | C-31, Table C-16 | verified | |
| IALU load/store, alternate access | C-32..C-36, Tables C-17..C-19 | verified | |
| IALU immediate extension | C-37, Table C-20, Fig C-10 | fixed | ext = value>>8 |
| Direct jump/call | C-38..C-39, Table C-21 | verified | |
| Indirect jump, condition, static flags, RDS | C-40..C-42, Table C-22 | verified | static flags follow C-22 |
| Condition codes | C-43..C-45, Tables C-23..C-25 | verified | |
| Sequencer immediate extension | C-45..C-46 | verified | |
| NOP/IDLE/BTB/TRAP/EMUTRAP | C-46..C-48 | verified | |

## Syntax (Appendix A, chapters 1, 8)

| Item | Manual | Status |
|---|---|---|
| Unprefixed compute register = XY (both blocks) | 2-7, 7-10, A-2.., Listing 8-1 | fixed |
| Unprefixed compute conditions / static flags = XY | 3-15, C-44, A-15 | fixed |
| `{X|Y}STAT` transfers reject XY | A-10, 10-195 | fixed |
| Block prefix repeated on operands (`XR0 = FIX XFR3:2`) | 10-70, 10-93, 10-181 | fixed |
| `IF cond, DO instr` (comma) and `()` empty option | 3-16, 5-23, 6-20, 7-14, 3-7 | fixed |
| Failed compute reading falls back to Ureg move (`XR3:0 = R7:4`) | 1-45 | fixed |
| CLU MAX/TMAX `Rmq_h`/`Rmq_l` written as pairs (`TR3:2 + R3:2, TR5:4 + R1:0`) | 10-97, 10-99 | fixed |
| Direct/indirect branch prediction default (BP=1), `(NP)` optional | C-39, A-14, 10-234 | fixed |
| `(NF)` on every slot of a sequencer-headed line -> header NF | A-14 fn 1, 8-16 | fixed |
| `(NF)` rejected where excluded (PASS/COMP/FCOMP, CLU/MR4/STAT/TR transfers, BITEST, BKFPT, data transfers) | 3-13, 4-43, 5-22, 6-19, 7-11 | fixed |
| NOP in any slot / several NOPs | C-42, 8-95, 8-99 | fixed |
| Parallelism Tables 1-1/1-2 ports, SOC single transfer | 1-31..1-37 | verified / fixed (SOC) |
| Ch. 1 Valid/Invalid labelled examples (41) | 1-38..1-48 | verified (test) |
| X ports I/O = 2 vs 1-45 valid "XR3:0 = R7:4 ; XR10 = [J17 + 2]" | 1-33/34 vs 1-45 | manual-conflict: 1-45 followed |
| Ch. 10 syntax listings expanded (9486 statements) | 10-1..10-250 | verified (same exclusions as App. A; 10-49 'BRsq = COMPACT' typo vs C-8/A-4) |
| Ureg registry vs Appendix B DEFTS201.H `_LOC` addresses (468) | B-1..B-42 | verified: 0 mismatches; added DSTATL/H, DSTATCH, CADATAx, IVBUSLOCK |
| Option bits of every corpus statement vs hand-entered Appendix C masks (5586) | C-5..C-28 | verified (tests/test_option_bits.py) |
| Pair shift immediate ranges by output operand (B4/S5/N6/L7) | A-9 note 2, 10-171 | fixed |
| Extended instruction in any slot, extension in slot 2 | 1-23 Fig 1-6 vs 7-42 advice | fixed |
| Ch. 1..9 syntax listings expanded (9584 statements) | 1-1..9-end | verified: only documented exclusions |
| `Rsd = ASHIFT Rmd BY Rnd` (6-22 prose) vs `BY Rn` (C-9, A-9, 10-171) | 6-22 | manual-conflict: single Rn kept (5-bit field) |
| `J31 = [address]` (8-71 stall table) vs JSTAT naming for transfers | 7-3/7-4, 8-71 | manual-conflict: 7-4 rule kept (use JSTAT) |
| Appendix A option corpus (8179 expansions of every listing) | A-2..A-15 | verified (tests/appendix_a_options.txt) |
| `{S|L}MRa = Rmd` without SE/ZE = SE | A-8, 10-152, C-17 | fixed |
| `MR4 = Rm (NF)` | A-8/10-152 vs 5-22 | manual-conflict: listing followed |
| MAC `(I)(CR)` | A-7 vs 5-17 | manual-conflict: rejected per 5-17 |
| One-slot 15-bit direct branch when the extension slot is needed/full | 1-35, C-39, C-42, 8-95 | fixed |
| Predicted PC-relative CJMP (only CJMP_CALL needs ABS) | 8-16, 10-236, 10-242 | fixed / manual-conflict |
| `CJMP = expr` / `RETI = ...` are Ureg transfers, not branches | 8-93 | fixed |
| Expressions: `ADDRESS()`, `* / % << >> & \| ^ ~` | 7-40, 7-45, 8-93, A-16 | fixed |
| Float immediates in Ureg loads (`XR0 = 9.0`) | 10-86, 10-88 | fixed |
| `CACMDx = Jmd` long transfer (CACMDx + CCAIRx) | A-17, 9-77 | fixed |
| CLU register load source only naturally aligned (`XYTR1:0 = R3:2`) | C-25 note 1, 10-198 | fixed / manual-conflict |
| XY in register-to-register moves (`XYR0 = CJMP` on 1-30 vs 1-44 rule) | 1-30, 1-44 | manual-conflict: 1-44 rule kept |
| Dual add/sub second result `Ra` without type prefix | A-3 | fixed |
| `STAT_L` spelling | A-10 | fixed |

## Open items

- Manual example lines still rejected on purpose (sweep of 626 `;;` lines):
  marked-invalid examples (ch. 1), placeholders (`Rsd = PR1:0`, `[Address]`),
  typos (`XRO`, `R30:29`, `TR15:4`, en-dash operators, `COMP (J4, 0x0000 0003)`,
  `IF NALE JUMP -50` without comma), `FDEP ... =` instead of `+=` (6-11/6-17
  vs every syntax listing), `XR31 += ASHIFT` (10-235), and header constants
  (`INT_GIE`) that need an include facility.

## Log

| Commit | Change |
|---|---|
| 9737a87 | Ureg imm32 extension = value>>8 (C-37) |
| 821fcc2 | manual branch prediction default, NOP slots, line NF |
| 73e15d9 | reject (NF) where the manual excludes it |
| 8c215bb | accept unprefixed (XY) and manual example spellings |
| 3d274f1 | accept manual example forms found by the example sweep |
| cf34c5e | listing options for SMR/LMR loads, MR4 NF; option corpus |
| 7593772 | one SOC Ureg transfer per line; ch. 1 example regression |
| 907a798 | Ureg names from PR Appendix B; registry cross-check |
| 8ed6076 | option-bit worksheet over corpus (no discrepancies) |
| bd3d2b8 | pair shift immediates limited to output operand width |
| 0080c81 | extended instruction may use any slot |
| ca07a7e | manual syntax sample added (examples/manual_syntax.asm) |
| 0d2f141 | ch. 1..9 listing sweep recorded |
| c12ba97 | block-prefixed STAT source |
