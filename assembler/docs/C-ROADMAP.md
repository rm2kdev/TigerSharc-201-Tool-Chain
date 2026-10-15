# Practical routes to C after milestone 1

Expansion update: scalar X/Y arithmetic, conversions, CALL/CJMP return, wide
transfers and several parallel arithmetic forms now exist in the assembler.
That removes some code-generation prerequisites below, but does not implement
a C frontend, ABI, register allocator, nested-call stack convention or runtime.
All data-model and compiler-route choices below remain proposals.

Assessment date: 2026-09-25. **No C compiler or compiler framework is implemented
or adopted by this milestone.** Recommendations below are engineering judgments,
not demonstrated backend capabilities or performance promises.

## Route comparison

| Route | What it reuses | Work and risk for TS201 | Assessment |
|---|---|---|---|
| Restricted C frontend + small typed IR | A small parser, explicit types/control flow; this assembler as code sink | Language semantics, diagnostics, register allocation, ABI and runtime all ours; deliberately limited scope | Fastest route to a reviewable integer/pointer compiler experiment |
| GCC target port | C frontend, optimizers, RTL, register allocation, scheduling infrastructure | Machine descriptions, register classes, calling/stack hooks, word storage model, assembler driver, runtime and tests; large maintenance commitment | Strong long-term candidate if broad C compatibility is required |
| LLVM/Clang target port | C frontend, IR optimization, instruction selection and machine-code framework | Target registration, lowering, legalizer, instruction/register descriptions, ABI, MC and scheduling; **word-addressed C data model is a separate feasibility gate** | Promising infrastructure, not just adding instruction patterns |
| QBE target + frontend | Smaller backend, SSA and register allocation | Still needs a C frontend, target ABI/codegen and storage-model work; its documented small data types use 8/16-bit units | Worth a bounded prototype, not an existing TS201 solution |

GCC documents `BITS_PER_UNIT` as target-configurable and has hooks for pointer
width and alignment. This is favorable for a 32-bit addressable C unit, but does
not prove all compiler/library paths work with one. Sources:
[machine modes](https://gcc.gnu.org/onlinedocs/gcc-12.2.0/gccint/Machine-Modes.html),
[storage layout](https://gcc.gnu.org/onlinedocs/gccint/Storage-Layout.html).

LLVM documents the substantial components of a backend; its data-layout and
memory operations use byte-based sizes. A word-addressed implementation must
prove correct representation/lowering for pointers, GEP, aggregates, memcpy and
debug information, and address Clang's C `char` model. A 32-bit pointer-width
setting alone does not solve this. Sources:
[backend guide](https://llvm.org/docs/WritingAnLLVMBackend.html),
[IR data layout](https://llvm.org/docs/LangRef.html#data-layout).

QBE's IL defines 32/64-bit integer and floating types and 8/16-bit subword data
types; its pointer representation and ABI are target-specific. Adapting it would
still require a target and frontend/storage design. Source:
[QBE IL](https://c9x.me/compile/doc/il.html).

Recommendation: next prove a small typed integer/pointer C subset through this
assembler while specifying an independent ABI. Use those programs as the
acceptance corpus for later framework experiments. Before committing to GCC,
LLVM or QBE, compile the same pointer/struct/call probes on a prototype and compare
maintenance complexity. Do not spend months on a broad frontend before validating
its smallest addressable unit and target ABI.

## Proposed initial data model (not implemented)

Use a freestanding, word-addressed profile: `CHAR_BIT=32`, `char`, `short`, `int`
and `long` stored in one 32-bit word, pointers and `size_t` 32-bit, `ptrdiff_t`
signed 32-bit. Consequently `sizeof(int)==1` and `int *p; p+1` advances one DSP
word. This differs from host C and must be reflected in all type/layout tests.
Each string character occupies a word; packed external octets need explicit
serialization helpers. An `uint8_t` typedef cannot be promised when there is no
8-bit C object type.

The first subset should reject `long long`, floating types, bitfields, unions,
variadics, function pointers, atomics, dynamic allocation and undefined constructs
it cannot diagnose/represent safely. Later specify 64-bit integers as two words,
binary32 float, and binary64 double via tested helpers where necessary. The
existing project's vendor probes use 32-bit char and 32-bit double; choosing
binary64 double means intentionally **no vendor ABI compatibility** for that type.
An explicitly named double32 extension profile could come later; it must not be
silently presented as full conforming C. Define multiword order and alignment in
the ABI rather than inheriting host layout.

Define unsigned wrap modulo 2^32. Preserve C signed-overflow rules; the assembler's
hardware wrap is not a license for the optimizer to invent source semantics.
Treat MMIO through explicit volatile register and memory-access primitives only
after the corresponding instructions are supported. Loading internal Ureg
addresses through ordinary memory is not a substitute for proper TS201
register-transfer instructions.

## Calling convention, allocation and stack

An independent ABI can start from the familiar scalar argument shape in the
workspace notes (J4..J7, return J8) without claiming interoperability. Before calls
are accepted, publish the complete caller/callee-saved sets, outgoing argument
area, stack direction, return-address save, recursion and aggregate conventions.
A candidate is J27 as a downward-growing stack in a dedicated bank and four-word
frame alignment to leave room for later quad saves. Reserve scratch and stack
registers explicitly; J31/K31 are not ordinary storage registers. Save CJMP before
a nested call. Add source-level calls, CALL/CJMP return assembly support and
canary tests together. Do not transplant the vendor's dual J/K frame conventions
or the observed fifth-argument offset without a complete independently specified
ABI. Interrupt entry will need its own full context convention.

Begin with a simple spill-based or linear-scan allocator, J register class only,
and predictable scalar stack slots. Then add K address/integer registers and
explicit transfers between classes. X/Y computation registers need separate
constraints, paired/quad allocation and bank-aware data movement. An optimizer
must model flags as dependencies so it cannot move a compare across a flag writer.

Runtime helpers must be original or come from compatible open-source code with
clear attribution: integer division/remainder, multiword arithmetic and shifts,
conversions, memory/string operations, initialization/zeroing, and later math.
No emitted helper symbol may silently resolve to ADI libdsp/libc/startup objects.
Link failures should list missing helpers. Each helper needs boundary tests
against an independent reference implementation, including divide-by-zero policy.

## Floating point and realtime performance

First add scalar X/Y loads, moves, float arithmetic, comparisons and conversions
with manual-derived encoding tests and independent numeric references. Pin the
rounding mode and behavior for NaNs, infinities, subnormals, signed zero and
overflow. The CPU's 40-bit extensions and any approximate host math paths require
separate audits. Distinguish binary32 storage from intermediate precision. Avoid
fast-math reassociation in the correctness baseline.

A correctness-first compiler can initially emit one operation per packet. That
does not make it competitive for an audio DSP. Realtime performance later needs
latency/resource models, instruction selection for MAC/SIMD, register pressure
control, software pipelining, bank-aware placement, circular buffers and DMA
coordination. A packetizer must enforce all resource/slot/write conflicts and
extension constraints; scheduling must respect dependent operations and flags.
Benchmark FIR, biquad, oscillator and nonlinear kernels against independently
written references and, eventually, cycle counters on hardware. Instruction packet
counts are not silicon cycles or evidence of an audio deadline.

## Concrete next acceptance gates

1. Add independent CALL/return/Ureg transfers and an ABI test with nested calls,
   spills and a stack canary. Keep the assembler rejection tests.
2. Compile original restricted C loops, local scalars, arrays and pointer indexing;
   compare output to a host reference using explicit 32-bit arithmetic.
3. Add two modules, exported symbols and relocation tests before fixing an object
   format. Check all relocation units and packet targets after placement.
4. Implement/verify missing runtime helpers and scalar floating point; only then
   grow language coverage and scheduling.
5. Treat target initialization, image deployment and hardware performance
   as later milestones with independent hardware validation.
