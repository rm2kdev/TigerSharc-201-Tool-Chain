# Backlog

Claim an item by creating `agent/<topic>` (see AGENTS.md); `git branch --list
"agent/*"` shows what is taken. Delete the item in the branch that finishes it.
Add new items with enough context that a fresh agent can start cold.

## Compiler correctness

- **Fuzz findings that also fail on main 0c55a60 / cdbc30e** (historical differential
  runs; source and seed tooling remain in those revisions): mismatches 6155 (wp), 11120 (wp), 11476
  (extended), 12209 (wp), 13095 (default), 20308 (extended, also on
  d0f2c85); runs 20486 (wp) and 30312 (default) "parallel memory accesses
  overlap with a store", 31018 (extended, O2 access outside declared
  data), all also on d0f2c85; o0 30492 (reference run leaves declared
  data); runs 12170 (wp), 13456
  (extended) "parallel memory accesses overlap with a store"; crash 12221
  (wp, `ValueError: B60 is not in list`). Seed 12040 (extended) calls
  `memcpy(gp10 + 2, &pool7[1], 5)` with `gp10 == pool7`: overlapping
  memcpy is undefined, so the generator should avoid it.
- **Extended profile rejects union assignment and union copy-initialisation** -
  `union U{int a[2];unsigned b;}; union U x,y; ... x=y;` fails with
  "incompatible assignment or argument type", `union U y=x;` with "array
  initializer needs braces" (struct equivalents work). Valid C; add standalone regression cases when implementing support.

## Compiler performance

- **SIMD X/Y pairing** - pair independent X and Y compute operations into
  single instructions in instruction-word-bound loops.
- **Cheaper loop bounds checks and pipeline setup** - reduce repeated range
  checks and software-pipeline prologues and epilogues.
- **`split_ialu` J/K balance** - account for dependencies between inlined
  arithmetic, address calculations and stores when choosing an IALU block.
- **Faster variable division** - reduce packet counts in division-heavy loops.
- **Bulk memory operations** - improve generated `memcpy` sequences while
  preserving aliasing rules and source semantics.
- **Struct member array ranges** (branch `agent/dsp-codegen-member-ranges`,
  wip, pre-rebase): accesses to `s->arr[i]` cannot touch other fields, so scalar
  fields need not be reloaded after array stores. Earlier experiments improved
  some kernels but increased register pressure elsewhere; account for that
  tradeoff before enabling the optimization.
- **Constant store data** - a constant stored in a loop is loaded into a fresh
  register at every store. Hoisting the value or loading it once per block
  miscompiled historical fuzz seed 12492 and was reverted; diagnose the cause.
- **Hub split cost model** - `isel.split_ialu` estimates per-block load without
  considering the final schedule. Account for extra entry packets and transfers
  when evaluating a split.
- **J/K balance for fields of one base pointer** - address chains can use copies
  on both IALUs, but `[s + const]` accesses remain on the original hub's unit.
  A per-block load estimate did not solve local imbalances; choose the base
  register with scheduling information.
- **Masked-index alias ranges** - `p + (x & m)` lies in `p .. p + m`, which can
  let field loads move past independent stores. An `Addr.span` prototype in
  `opt.address_of` passed checks but worsened some schedules. Improve scheduler
  priorities before enabling the additional freedom.
- **Y compute block in straight-line kernels** - `split_compute` currently runs
  only on hot loops. Independent clamp chains can otherwise serialize on X.
- **Adjacent field loads as L/Q transfers** - require provable pointer alignment,
  such as a suitable attribute or a known-aligned global.
- **Selects on distinct flags** - consecutive if-converted selects currently
  use one unit's flags, requiring one comparison per packet.
