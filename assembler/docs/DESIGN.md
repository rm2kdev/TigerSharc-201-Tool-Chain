# Image interface and decisions

## Image interface

Images use an explicit region manifest and a companion symbol/packet map.
Section addresses are DSP words; binary files contain little-endian 32-bit
words. This interface does not define a target startup protocol.
Source labels are resolved in two passes. Relocatable modules are documented
separately in `linker/OBJECT-FORMAT.md` at the repository root.

## Output contract (v1)

Each named section has `<name>.bin`, containing tightly packed 32-bit words in
little-endian host-file octets. There is no header, implicit gap, relocation, or
padding between files. Zero-filled data is explicit bytes in its own region.

`regions.json` is an array of section descriptors:

```json
[
  {
    "file": "values.bin",
    "name": "values",
    "kind": "data",
    "start_word_address": "0x00040000",
    "end_word_address_exclusive": "0x00040001",
    "words": 1,
    "bytes": 4,
    "sha256": "<64 lowercase hexadecimal characters>"
  }
]
```

A target loader should validate file sizes and hashes before placing each
region at its declared word address.
`map.json` has format `ts201-map-v1`, address unit, byte order, numeric entry word
address, numeric symbol word addresses, regions, and a packet table. Each packet
contains numeric `word_address`, `word_count`, one-based source `line`, and `text`.
`symbols.map` is the human-readable companion. No timestamp or absolute source
path enters an image. Source-line text is kept for review; it is not executable
host code. Only ASCII identifiers are allowed in section filenames.

For section base B and target word A:

```text
binary offset in host octets = (A - B) * 4
target symbol = A
target branch displacement = destination_word - packet_start_word
```

The assembler accepts only these half-open silicon ranges:

| Bank | First word | End word (exclusive) |
|---|---:|---:|
| M0 | 0x000000 | 0x020000 |
| M2 | 0x040000 | 0x060000 |
| M4 | 0x080000 | 0x0A0000 |
| M6 | 0x0C0000 | 0x0E0000 |
| M8 | 0x100000 | 0x120000 |
| M10 | 0x140000 | 0x160000 |

Authority: [ADI Hardware Reference](https://www.analog.com/media/en/dsp-documentation/processor-manuals/396096833ts201_hwr.pdf),
printed 2-1..2-2, Figure 2-1. Each section must fit within one bank. No external
capacity, startup reservation, or application-defined address alias is inferred.
These images assume otherwise unallocated RAM. Integration with other programs
must reserve vectors, stacks, runtime storage and DMA ownership for each DSP
independently.

## Future module boundary

Keep symbols and expressions semantic until final placement. A next object
design should store sections, alignment in words, typed symbols/imports/exports,
and relocations carrying explicit units and checked ranges. Needed relocation
kinds include ABS32_WORD data, UREG_IMM32 (split imm15 plus extension), and
PCREL32_WORD / ABS32_WORD_BRANCH (split imm15 plus branch extension). Relocation
patches must preserve opcode, EX and packet boundaries; a relaxation pass must
recompute all addresses. Cross-module branches need packet-start validation after
linking. Reject duplicate exports, unresolved imports and cross-bank allocations.

Do not freeze a home-grown on-disk object format prematurely. First write a
two-module test with a data-pointer relocation and a call/return relocation, then
decide whether the simplest intermediate representation or a documented ELF
target convention best serves GCC/LLVM integration. No vendor ELF metadata or
runtime is necessary for the present interface.

## Verification boundary

The Python tests check literal encodings, packet constraints, layout, hashes,
symbols and image reproducibility. Corpus builds emit images without executing
target instructions. Dynamic address bounds, register results, peripheral
protocols and timing require separate validation on the intended target.
