import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from ts201.assembler import AssemblyError, BANKS, assemble, expression


def program(body, base=0x100, data=""):
    return f".entry start\n.section text, code, {base}\nstart:\n{body}\n{data}"


class EncodingTests(unittest.TestCase):
    # Literal, manually derived vectors. Do not generate these via assembler
    # functions or implementation decoders. Field worksheets: docs/ENCODINGS.md.
    VECTORS = {
        "NOP;;": [0xB3C00000],
        "IDLE;;": [0xB3C40000],
        "J0 = 1;;": [0x89808001],
        "K2 = -1;;": [0x99A2FFFF],
        "J7 = 0x12345678;;": [0x0987D678, 0xB2123456],
        "K7 = 0x12345678;;": [0x19A7D678, 0xF2123456],
        "J3 = J1 + J2;;": [0x8C030104],
        "K3 = K1 - -128;;": [0x9C232180],
        "J2 = J0 + 127 (NF);;": [0xCC02207F],
        "J4 = J1 OR J2;;": [0x8E440104],
        "J4 = J1 AND J2;;": [0x8E640104],
        "J4 = J1 XOR J2;;": [0x8E840104],
        "COMP(J1, J2);;": [0x8D800104],
        "COMP(K3, 0) (U);;": [0x9DA02300],
        "J2 = [J4 + 3];;": [0x81822403],
        "[K4 += -1] = K2;;": [0x95A2A4FF],
        "J2 = [J4 + J5];;": [0x8182040A],
        "J2 = J3;;": [0x89830C04],
        "J0 = J1 + 2; K0 = K1 - 3;;": [0x0C002102, 0x9C202103],
        "JUMP start (NP);;": [0x31080000, 0xB3800000],
        "IF NJEQ, JUMP start (NP);;": [0x31400000, 0xB3800000],
        "JUMP start (NP)(ABS);;": [0x30080100, 0xB3800000],
    }

    def test_direct_address_of_external_symbol(self):
        # PR 7-15 / 8-84: `[J31 + external_address]`. A symbolic direct address
        # may lie in a configured external window; a literal one must be internal.
        windows = BANKS + ((0x40800000, 0x40810000),)
        source = (".entry start\n.section text, code, 0x100\nstart:\n"
                  "XR0 = [J31 + value + 1];;\n[K31 + value] = XR0;;\nIDLE;;\n"
                  ".section far, data, 0x40800000\nvalue:\n.word 5, 6\n")
        assemble(source, memory_windows=windows)
        with self.assertRaisesRegex(AssemblyError, "outside internal RAM"):
            assemble(program("XR0 = [J31 + 0x40800000];;"), memory_windows=windows)

    def test_manual_vectors(self):
        for source, words in self.VECTORS.items():
            with self.subTest(source=source):
                self.assertEqual(assemble(program(source)).sections[0].words, words)

    def test_signed_constant_boundaries(self):
        for value, count in [(-16385, 2), (-16384, 1), (16383, 1), (16384, 2),
                             (-2147483648, 2), (4294967295, 1), (2147483647, 2)]:
            with self.subTest(value=value):
                self.assertEqual(len(assemble(program(f"J0 = {value};;")).sections[0].words), count)

    def test_extension_field_nonoverlap(self):
        for value in (0x1234ABCD, 0x80008000, 0xFFFF80FF, 0x7FFFFFFF):
            for unit in ("J", "K"):
                with self.subTest(value=value, unit=unit):
                    words = assemble(program(f"{unit}4 = {value};;")).sections[0].words
                    # PR C-37 Fig C-10: the extension holds value bits 31:8
                    # (J7 = 0x2A0 -> 0x000002); imm15 holds bits 14:0, so the
                    # overlapping bits 14:8 agree in both words.
                    self.assertEqual(words[1] & 0xFFFFFF, value >> 8)
                    self.assertEqual(((words[1] & 0xFFFFFF) << 8) | (words[0] & 0xFF), value)
                    self.assertEqual((words[1] << 8) & 0x7F00, words[0] & 0x7F00)
                    self.assertEqual((words[1] >> 30) & 1, int(unit == "K"))

    def test_negative_relative_branch(self):
        words = assemble(program("NOP;;\nJUMP start (NP);;")).sections[0].words
        self.assertEqual(words, [0xB3C00000, 0x31087FFF, 0xB381FFFF])

    def test_forward_labels_and_word_offsets(self):
        image = assemble(program("J0 = value + 1;;\nJUMP end (NP);;\nNOP;;\nend: IDLE;;",
            data=".section data, data, 0x40000\nvalue: .word end, value + 1"))
        self.assertEqual(image.symbols, {"start": 0x100, "end": 0x105, "value": 0x40000})
        self.assertEqual(image.sections[0].words[:2], [0x09808001, 0xB2000400])
        self.assertEqual(image.sections[1].words, [0x105, 0x40001])

    def test_packet_unaligned_and_cross_quad(self):
        image = assemble(program("J0 = 0x12345678;;\nIDLE;;", base=0x103))
        self.assertEqual([p["word_address"] for p in image.packets], [0x103, 0x105])
        self.assertFalse(image.sections[0].words[0] >> 31)
        self.assertTrue(image.sections[0].words[1] >> 31)

    def test_align_word_units(self):
        image = assemble(program("NOP;;\n.align 4\nnext: IDLE;;", base=0x101,
            data=".section data, data, 0x40001\na: .word 1\n.align 4\nb: .word 2"))
        self.assertEqual(image.symbols["next"], 0x104)
        self.assertEqual(image.sections[0].words[:3], [0xB3C00000] * 3)
        self.assertEqual(image.symbols["b"], 0x40004)
        self.assertEqual(image.sections[1].words, [1, 0, 0, 2])

    def test_all_six_bank_endpoints(self):
        for lo, hi in BANKS:
            for base in (lo, hi - 1):
                with self.subTest(base=base):
                    self.assertEqual(assemble(program("IDLE;;", base)).entry_word_address, base)

    def test_case_comments_expressions(self):
        image = assemble(program("/* comment\n continued */ j0 = -(1 + 2);; // trailing\nDoNe: iDlE;;"))
        self.assertEqual(image.sections[0].words[0], 0x8980FFFD)
        self.assertIn("DoNe", image.symbols)

    def test_nf_and_compare_parallel(self):
        image = assemble(program("COMP(J0, 0); COMP(K0, 0) (U);;\nJ0 = J0 + 1 (NF); K0 = K0 + 1 (NF);;"))
        self.assertEqual(image.sections[0].words, [0x0D802000, 0x9DA02000, 0x4C002001, 0xDC002001])

    def test_expression_no_eval(self):
        self.assertEqual(expression("label - 2 + (+1)", {"label": 17}), 16)
        # C-style operators and ADDRESS() are supported (PR 7-40, 7-45, A-16).
        self.assertEqual(expression("(1 << 3) | 2*3 - 7/2 + ADDRESS(label) % 5 ^ ~0", {"label": 17}),
                         ((1 << 3) | 6 - 3 + 2 ^ ~0))
        self.assertEqual(expression("-7 / 2"), -3)
        for bad in ("__import__('os')", "True", "1.2", "x.y", "[1]", "1 / 0", "1 << 99",
                    "ADDRESS(a, b)", "len(x)", "2 ** 3"):
            with self.subTest(bad=bad), self.assertRaises(AssemblyError):
                expression(bad)


class RejectionTests(unittest.TestCase):
    def test_invalid_packets(self):
        cases = {
            "J0 = J1 + J2; J3 = J4 + J5;;": "duplicate IALU",
            "J0 = J1 + J2; J0 = J3 - J4;;": "duplicate IALU",
            "J0 = 0x12345678; K0 = 0x12345678;;": "one immediate extension",
            "J0 = 1; K0 = 2; J1 = 0x12345678; K1 = 0x12345678;;": "exceeds four",
            "J0 = [J0 += 1];;": "same register twice",
            "J0 = [J31 += 1];;": "post-modify J31",
            "J0 = J1 + K2;;": "mixed J/K",
            "J0 = K1 + K2;;": "mixed J/K",
            "J0 = [J1 + K2];;": "mixed J/K",
            "J0 = J1 + 4294967296;;": "32-bit word",
            "J0 = [J1 + -0x80000001];;": "32-bit word",
            "J0 = [J31 + -1];;": "outside internal RAM",
            "J31 = 1;;": "status-register",
            "J1 = J31;;": "status-register",
            "[J0 + 0] = J31;;": "status-register",
            "J32 = 1;;": "unsupported register",
            "J0 = 4294967296;;": "32-bit word",
            "J0 = -2147483649;;": "32-bit word",
            "IF XBAD, JUMP start (NP);;": "unknown condition",
            "NOP; IDLE;;": "one sequencer instruction",
            "X R0 = YR1 + YR2;;": "another compute block",
            "Q[J0 + 1] = J3:0;;": "must be aligned",
            "J0 = J1 * J2;;": "register",
            "CALL start (NP)(BP);;": "contradictory",
            "J0 = 0x12345678; ; K0 = 1;;": "empty operation",
            "IDLE;": "complete packet",
            "NOP;; IDLE;;": "complete packet",
            "J0 = 1;\nK0 = 2;;": "complete packet",
            "J0 = missing;;": "undefined symbol",
            "JUMP start + 1 (NP);;": "packet start",
        }
        for body, message in cases.items():
            with self.subTest(body=body), self.assertRaisesRegex(AssemblyError, message):
                assemble(program(body), "bad.asm")

    def test_invalid_layouts(self):
        cases = [
            (program("IDLE;;", 0x20000), "reserved gap"),
            (program("IDLE;;", 0x3FFFF), "reserved gap"),
            (program("IDLE;;", 0x160000), "reserved gap"),
            (program("IDLE;;", 0x40000000), "external memory"),
            (program("IDLE;;", -1), "outside TS201"),
            (program("J0 = 0x12345678;;", 0x1FFFF), "bank boundary"),
            (program("IDLE;;", data=".section d, data, 0x100\n.word 0"), "overlapping"),
            (program("IDLE;;", data=".section text, data, 0x40000\n.word 0"), "duplicate section"),
            (program("IDLE;;", data=".section CON, data, 0x40000\n.word 0"), "reserved on Windows"),
            (program("start: IDLE;;"), "duplicate symbol"),
            (program("J0: IDLE;;"), "register name"),
            (program(".word 0xB3C40000"), "data-only"),
            (program(".zero 1"), "data-only"),
            (program("IDLE;;\n.align 3"), "power of two"),
            (program("IDLE;;\n.align 0"), "power of two"),
            (program("IDLE;;\n.align 0x40000"), "power of two"),
            (program("IDLE;;", data=".section d, data, 0x5FFFF\n.word 1,2"), "bank boundary"),
            (program("IDLE;;", data=".section d, data, 0x40000\n.word 0x100000000"), "32-bit word"),
            (program("IDLE;;", data=".section d, data, 0x40000\n.word 1,"), "empty .word"),
            (program("IDLE;;", data=".section d, data, 0x40000\n.zero -1"), "count"),
            (program("IDLE;;", data=".section d, data, 0x40000\nIDLE;;"), "code section"),
            (program("IDLE;;\n.entry start"), "duplicate .entry"),
            (".section t, code, 0\nIDLE;;", "missing .entry"),
            (".entry x\n.section t, data, 0\nx: .word 1", "entry must"),
            (".entry x\n.section t, code, 0\nx:", "empty section"),
            ("label: IDLE;;", "before a section"),
            (program("/* unclosed"), "unterminated"),
            (program("JUMP d (NP);;", data=".section d, data, 0x40000\nd: .word 0"), "packet start"),
        ]
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(AssemblyError, message):
                assemble(source)

    def test_diagnostic_has_filename_line(self):
        with self.assertRaisesRegex(AssemblyError, r"hello.asm:4:"):
            assemble(program("J0 = J1 + 4294967296;;"), "hello.asm")


class ImageTests(unittest.TestCase):
    def test_image_integrity_and_reproducibility(self):
        image = assemble(program("J0 = value;;\nIDLE;;", data=".section data, data, 0x40000\nvalue: .word 0x12345678"))
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / "a", Path(temp) / "b"
            image.write(a)
            image.write(b)
            self.assertEqual({p.name: p.read_bytes() for p in a.iterdir()}, {p.name: p.read_bytes() for p in b.iterdir()})
            regions = json.loads((a / "regions.json").read_text())
            for region in regions:
                raw = (a / region["file"]).read_bytes()
                self.assertEqual(len(raw), region["words"] * 4)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), region["sha256"])
            self.assertEqual((a / "data.bin").read_bytes(), b"\x78\x56\x34\x12")
            self.assertEqual(struct.unpack("<3I", (a / "text.bin").read_bytes()), (0x09808000, 0xB2000400, 0xB3C40000))
            with self.assertRaisesRegex(AssemblyError, "already exists"):
                image.write(a)

    def test_every_packet_has_one_terminal_ex(self):
        for source in (Path(__file__).parents[1] / "examples").glob("*.asm"):
            image = assemble(source.read_text())
            for p in image.packets:
                s = next(s for s in image.sections if s.start_word_address <= p["word_address"] < s.start_word_address + len(s.words))
                offset = p["word_address"] - s.start_word_address
                words = s.words[offset:offset + p["word_count"]]
                self.assertTrue(1 <= len(words) <= 4)
                self.assertEqual([w >> 31 for w in words], [0] * (len(words) - 1) + [1])


if __name__ == "__main__":
    unittest.main()
