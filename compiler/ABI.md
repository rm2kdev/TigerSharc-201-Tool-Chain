# TS201 word32 stack ABI v1

Identifier: `ts201-word32-stack-v1`. Independent ABI; **no ADI binary compatibility
claim**. Object format: `ts201-symbolic-object-v1` in the sibling linker.

## Data

The smallest C addressable unit is one 32-bit DSP word: `CHAR_BIT=32`,
`sizeof(char)==sizeof(short)==sizeof(int)==sizeof(long)==sizeof(void*)==1`.
`_Bool`, `size_t` and `ptrdiff_t` also occupy one word. Plain char is signed;
signed integers are two's complement. Unsigned arithmetic wraps modulo 2^32.
Signed right shift is arithmetic. Casts to signed integer interpret the low
32 bits in two's complement; casts to unsigned preserve low 32 bits.
`_Bool` conversions yield 0 or 1.

Pointers contain DSP word addresses, not host byte offsets. Array stride is
the element's word size. Struct fields are laid out consecutively with
one-word alignment and no padding; all supported members have one-word
alignment. A string character occupies one word, followed by a zero word.
No 8/16-bit integer object typedefs are supplied. Serialize external octets
explicitly rather than assuming host byte layout. Host `.bin` files store each
word as four little-endian octets.

## Registers and calls

| Role | Convention |
|---|---|
| J27 | Downward-growing, four-word-aligned stack pointer |
| J26 | Callee-saved; `-O0` code uses it as a frame pointer |
| CJMP | Hardware return address; saved by functions that make calls |
| XR0 | Scalar or pointer return value; unspecified for void |
| XR0..XR15, YR0..YR15, J0..J11, K0..K15 | Caller-clobbered scratch registers (`-O0` code only uses XR0..XR2 and J0..J8) |
| XR16..XR31, YR16..YR31, J12..J30 (except J27), K16..K30 | Callee-saved; optimized code allocates these and saves/restores those it uses |
| JSTAT/KSTAT/XSTAT | Caller-clobbered flags |
| LC0, LC1 | Caller-clobbered: optimized code uses them for hardware loops and division |

The caller allocates `round_up(argument_count,4)` words by subtracting that
amount from J27. Argument i occupies `[J27+i]`, one word per scalar or pointer.
Arguments evaluate left to right in this implementation; programs must still
obey C's rules about unsequenced side effects. Nested argument evaluation uses
lower stack addresses and restores the outgoing area. The caller removes its
argument area after the return.

The callee subtracts a frame size rounded up to four words. `-O0` code keeps
a frame-pointer chain: relative to its new J26, word 0 holds the old J26,
word 1 holds CJMP, and words 2 onward hold locals and spilled expression
temporaries; incoming argument i is at `J26 + frame_size + i`. On return it
restores CJMP, restores J27 to the caller's outgoing area, restores J26, then
executes `CJMP (NP)(ABS)`.

Optimized (`-O1`/`-O2`) code addresses its frame from J27 and does not keep a
frame-pointer chain. Relative to J27 after its prologue:

| Words | Contents |
|---|---|
| `0 .. A-1` | Outgoing argument area (A = largest call, rounded up to four) |
| `A` | Saved CJMP (only in functions that make calls) |
| next, four-word aligned | Saved callee-saved registers, in aligned pair/quad groups |
| then | Spill slots and address-taken locals |
| `F + i` | Incoming argument i (F is the frame size) |

Leaf functions that need no stack space adjust no registers at all. Function
calls store arguments directly into the outgoing area, so J27 does not move
around calls.

A call whose result is returned directly may become a tail call: the function
stores the callee's arguments into its own incoming argument slots (only when
they fit in the `round_up(parameter_count,4)` words its caller allocated),
restores its callee-saved registers and CJMP, releases its frame and jumps to
the callee, which returns straight to the original caller. Functions that pass
the address of a frame object never do this. A tail-called function sees the
same J27, arguments and return address it would see after a CALL from the
original caller, so assembly callees need no change as long as they only touch
their own `round_up(argument_count,4)` argument words.

Prologues are shrink-wrapped: when every call and every use of a
callee-saved register lies in a part of the function entered through one
block, the saves (and, when the other paths touch only incoming arguments,
the J27 adjustment) happen there, and early-exit paths return without a
frame. A return with nothing to restore may be a conditional
`IF cond, CJMP (ABS)`.

None of this changes argument placement, external symbol names, the return
register, stack alignment, or the ABI identifier. `-O0` and optimized objects
can be mixed, including C calls to existing assembly kernels, provided
assembly code preserves the callee-saved registers above and does not expect
LC0/LC1 to survive a call.

Optimized epilogues restore CJMP with one Ureg load, `CJMP = [J27 + A]`.
Computed jumps through jump tables use the IALU form `J0 = J0 + 0 (CJMP)`.

External C names receive `_c_`, e.g. `main` -> `_c_main`. File-private symbols
receive `_s_`; the linker separately namespaces every object's labels. Function
and data signatures accompany exported/imported C symbols and mismatches fail
at link time. Unused extern declarations do not require definitions. A complete
array definition can satisfy an incomplete extern array declaration.

## Startup and memory

Default code begins at word 0x100, data at 0x40000. A 4096-word stack region
begins at 0x140000. Its first word and last four words contain 0x51acce55 guards.
Startup sets J26=0 and J27 to the first high-guard word (one past the usable
stack), calls `_c_main`, then stops at `_halt: IDLE`. XR0 retains main's result.
Zero-filled static storage is present in the loaded image. Startup does not
configure clocks, peripheral devices, interrupts or external buses.

Only the assembler's six silicon internal banks may hold sections. The linker
checks sizes, alignment, bank boundaries, overlap, exports/imports, branch
targets and entry points. Runtime stack growth is not bounded in generated
machine code; configure a sufficient stack and validate runtime bounds during
development. Instructions rely on hardware register interlocks; there is no
software scheduling model or hardware performance certification.

## Reference basis

Supplied TS201 Programming Reference, Rev. 1.1: compute-register interlocking
(printed 2-6), J/K register and zero-operand behavior (7-3/7-4), IALU computed
jump result option (7-6/7-7), and sequencer CALL/CJMP instructions. The assembler
independently defines field encodings and silicon bank boundaries. This ABI is
our explicit software convention, not a convention inferred from those pages.
