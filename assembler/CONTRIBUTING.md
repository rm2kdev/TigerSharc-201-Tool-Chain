# Contributing

Run `python build.py` from `assembler/` before proposing a change. The
assembler, unit tests and corpus image build require Python 3.10+ only.
The workflow must remain offline and independent of ADI tools and licenses.

For an instruction addition:

1. Record the processor manual revision, printed encoding page/table and semantic
   page. Review reserved fields, sign extension, unit/register classes and flags.
2. Derive literal expected words on paper/in a field worksheet. Add at least one
   discriminating boundary vector, then an original executable program whose
   expected result comes from a separate arithmetic/semantic calculation.
3. Specify packet resources, read/write sets, slots and alignment before allowing
   the operation in parallel. Reject unimplemented combinations explicitly.
4. Include malformed operands, resource conflicts, addressing/width limits and
   a failure test. Update the README support table and provenance notes.
5. Run the full workflow. Resolve encoding conflicts against the cited manual
   tables and retain a focused regression case for each correction.

Use names ending in `_word_address`, `_word_count` and `_byte_offset` at address
boundaries. Host serialization uses four octets per target word; internal pointer
arithmetic never multiplies by four. SDAB-family operands explicitly use the
manual's short-address units; do not silently convert them to words or bytes.
Treat comparisons/flags separately from
arithmetic values. Test symbolic loads whose middle immediate bits are nonzero.

Optional vendor comparisons are **manual development experiments**, never build
steps or test prerequisites. Compile only original tiny source programs; compare
instruction words after accounting for intentional choices such as fixed-width
branches. Keep compiler version/options and a source hash with the result.
Do not import vendor executables, libraries, startup or runtime implementations.
There were no vendor comparisons in milestone 1. An optional expansion probe
failed the installed assembler's license check and yielded no comparison data.

Update docs/ISA-COVERAGE.md with encoded forms, execution evidence and remaining
gaps separately. `tests/appendix_a_cases.json` is a syntax inventory, not an
encoding oracle: retain its entry IDs and document any counting changes. Add
independent bit-field and semantic tests as well as acceptance cases. Register
facts in `ts201/registers.py` retain their manual table references. Changes to
packet rules must include positive parallel programs and negative conflicts.
Never turn a known correctness failure into a silent skip or describe a
static encoding check as a hardware conformance pass.

No blanket API, source, or ABI compatibility with VisualDSP++ is promised. New
syntax should be documented, deterministic and reject rather than reinterpret
unsupported constructs. Future compiler work must have explicit unsupported-C
diagnostics and separate functional and performance milestones.

License scope: LICENSE covers newly authored files in this directory. Do not
distribute recovered proprietary firmware, vendor SDKs or runtime code with
this toolchain.
