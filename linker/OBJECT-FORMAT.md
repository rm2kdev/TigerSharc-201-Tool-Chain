# Symbolic object format v1

`.tso` is UTF-8 JSON with `format: ts201-symbolic-object-v1` and
`abi: ts201-word32-stack-v1`. It deliberately stores semantic assembly records,
**not binary machine code with ELF relocations**. This keeps packet encodings
and immediate extension rules in the existing independently tested assembler.
It is a versioned intermediate object for this toolchain, not a frozen external
ecosystem format.

Fields:

| Field | Meaning |
|---|---|
| name | Diagnostic source basename |
| sections | Ordered records: name, code/data kind, power-of-two word alignment, lines |
| symbols | All local definitions with code/data kind |
| exports | Public symbol -> code/data kind |
| imports | Required external symbols |
| types | Optional C signature metadata for imports/exports |
| relocations | Section index, line index, symbol, semantic relocation kind |

Section lines are labels, data directives, alignments, or whole instruction
packets. `WORD_EXPR` marks a symbol in a data expression; `PACKET_EXPR` marks a
symbolic operand inside a packet. Indices are zero based. These relocation
records are derived from the lines and cross-checked on load. They identify
dependencies, not opcode bit ranges or byte offsets. Reconstructed definitions,
exports, imports, section lines and relocation records must all agree.

The linker calculates fixed packet lengths using the assembler's layout pass,
places sections on their requested word boundaries, assigns each object's
symbols a private namespace, resolves imports to exported definitions, then
passes the final placed assembly to `assemble`. Symbolic immediates keep their
fixed-size extensions; the one exception is a direct branch, which the linker
may mark `(S)` for the one-slot form once its placed target is known to fit
(see README). There is no binary patching.
The final assembler rechecks word widths, relative branch ranges, packet-start
targets, section boundaries and overlap. Thus an address/data relocation,
split universal-register immediate and split branch immediate all use the
assembler's established encoding paths after final placement.

All addresses, alignments, addends and displacements use 32-bit DSP words.
The linker accepts only the six internal banks already checked by the assembler:
[0,0x20000), [0x40000,0x60000), [0x80000,0xa0000),
[0xc0000,0xe0000), [0x100000,0x120000), [0x140000,0x160000).

There are no archives, weak/common symbols, overlay
sections, scatter scripts, ELF/DWARF, binary input, vendor libraries, external
memory inference, or flash packaging. Tentative C definitions become explicit
zero-filled definitions; duplicate definitions across modules fail. Header
declarations should use `extern` for shared objects.
