import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parent / "assembler")]
from cpu import CONSOLE_WORD, Cpu, CpuError, Image
from ts201.assembler import assemble


def literal_cpu(words):
    """Literal instruction words test decoding independently of the assembler."""
    memory = dict(enumerate(words, 0x100))
    starts, start = set(), True
    for address, word in memory.items():
        if start:
            starts.add(address)
        start = bool(word & 0x80000000)
    return Cpu(Image(memory, set(memory), starts, 0x100))


class CpuTests(unittest.TestCase):
    def run_source(self, source, limit=1000):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image"
            assemble(source).write(path)
            return Cpu(Image.load(path)).run(limit)

    def test_literal_ialu_add_wrap_and_zero_register(self):
        # PR C-13/C-14: J3 = J1 + J2; J31 = J1 + J2; IDLE.
        cpu = literal_cpu([0x8C030104, 0x8C1F0104, 0xB3C40000])
        cpu.registers[0][1:3] = [0xFFFFFFFF, 1]
        cpu.run()
        self.assertEqual(cpu.reg(0, 3), 0)
        self.assertTrue(cpu.zero[0])
        self.assertEqual(cpu.reg(0, 31), 0)

    def test_literal_k_subtract_signed_immediate(self):
        # PR C-13: K3 = K1 - -128.
        cpu = literal_cpu([0x9C232180, 0xB3C40000])
        cpu.registers[1][1] = 1
        cpu.run()
        self.assertEqual(cpu.reg(1, 3), 129)

    def test_literal_extended_constant(self):
        # PR C-16/C-20: J7 = 0x12345678, including overlapping imm15 bits.
        cpu = literal_cpu([0x0987D678, 0xB2123456, 0xB3C40000]).run()
        self.assertEqual(cpu.reg(0, 7), 0x12345678)

    def test_literal_scalar_load_and_postmodify_store(self):
        # PR C-17: J2 = [J4 + 3]; [K4 += -1] = K2.
        cpu = literal_cpu([0x81822403, 0x95A2A4FF, 0xB3C40000])
        cpu.registers[0][4] = 0x40000
        cpu.registers[1][4] = 0x40004
        cpu.registers[1][2] = 0x12345678
        cpu.memory[0x40003] = 0xDEADBEEF
        cpu.run()
        self.assertEqual(cpu.reg(0, 2), 0xDEADBEEF)
        self.assertEqual(cpu.memory[0x40004], 0x12345678)
        self.assertEqual(cpu.reg(1, 4), 0x40003)

    def test_hello_world_executes_to_idle(self):
        source = (HERE / "hello_world.asm").read_text()
        cpu = self.run_source(source, 76)
        self.assertEqual(cpu.output, b"Hello, world!\n")
        self.assertTrue(cpu.halted)
        self.assertEqual(cpu.packets, 76)
        self.assertEqual(cpu.reg(0, 0), 0x40000 + 15)
        self.assertEqual(cpu.reg(0, 1), 0)
        self.assertEqual(cpu.reg(1, 0), CONSOLE_WORD)
        with self.assertRaisesRegex(CpuError, "packet limit"):
            self.run_source(source, 75)

    def test_message_comes_from_image_data(self):
        source = (HERE / "hello_world.asm").read_text()
        source = source[:source.index("message:\n")] + "message: .word 66, 121, 101, 10, 0\n"
        self.assertEqual(self.run_source(source).output, b"Bye\n")

    def test_nf_and_short_backward_k_branch(self):
        cpu = self.run_source(""".entry start
.section text, code, 0x100
start: K0 = 3;;
J0 = -1;;
loop: K0 = K0 - 1;;
COMP(K0, 0);;
K1 = K1 + 1 (NF);;
IF NKEQ, JUMP loop (NP)(S);;
IDLE;;
""")
        self.assertEqual(cpu.reg(1, 0), 0)
        self.assertEqual(cpu.reg(1, 1), 3)
        self.assertEqual(cpu.reg(0, 0), 0xFFFFFFFF)
        self.assertTrue(cpu.zero[1])

    def test_absolute_jump_and_j_zero_branch(self):
        cpu = self.run_source(""".entry start
.section text, code, 0x100
start: J0 = 1;;
J0 = J0 - 1;;
IF JEQ, JUMP done (ABS)(NP);;
J1 = 99;;
done: NOP;;
IDLE;;
""")
        self.assertEqual(cpu.reg(0, 1), 0)

    def test_extended_offsets_and_add_immediate(self):
        cpu = self.run_source(""".entry start
.section text, code, 0x100
start: J0 = 0;;
J0 = J0 + 0x12345678;;
[J31 + value] = J0;;
K0 = [K31 + value];;
IDLE;;
.section data, data, 0x40000
value: .zero 1
""")
        self.assertEqual(cpu.reg(1, 0), 0x12345678)

    def test_unsupported_instruction_and_parallel_packet_fail(self):
        for words in ([0xFFFFFFFF], [0x0C002001, 0x9C002001],
                      [0x89808001, 0xB3022000]):
            with self.subTest(words=words):
                cpu = literal_cpu(words)
                with self.assertRaises(CpuError):
                    cpu.run()
        cpu = literal_cpu([0x09808001, 0x99A08002])  # J0=1; K0=2, same packet.
        with self.assertRaisesRegex(CpuError, "parallel"):
            cpu.step()
        self.assertEqual(cpu.reg(0, 0), 0)

    def test_invalid_memory_and_self_modifying_code_fail(self):
        cpu = literal_cpu([0xB3C40000])
        for address in (0x20000, 0x160000, CONSOLE_WORD):
            with self.subTest(address=address), self.assertRaises(CpuError):
                cpu.read(address)
        with self.assertRaisesRegex(CpuError, "self-modifying"):
            cpu.write(0x100, 0)
        with self.assertRaisesRegex(CpuError, "invalid RAM write"):
            cpu.write(0x20000, 0)
        cpu.write(CONSOLE_WORD, 0x141)
        self.assertEqual(cpu.output, b"A")

    def test_branch_into_extension_and_unbounded_loop_fail(self):
        cpu = literal_cpu([0x31088001, 0xB3800000, 0xB3C40000])
        with self.assertRaisesRegex(CpuError, "not a packet start"):
            cpu.run()
        cpu = literal_cpu([0x31080000, 0xB3800000])
        with self.assertRaisesRegex(CpuError, "packet limit"):
            cpu.run(5)
        self.assertEqual(cpu.packets, 5)
        with self.assertRaisesRegex(CpuError, "positive"):
            cpu.run(0)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "image"
        assemble((HERE / "hello_world.asm").read_text()).write(self.path)

    def change_manifest(self, change):
        regions = json.loads((self.path / "regions.json").read_text())
        image_map = json.loads((self.path / "map.json").read_text())
        change(regions)
        image_map["regions"] = regions
        (self.path / "regions.json").write_text(json.dumps(regions))
        (self.path / "map.json").write_text(json.dumps(image_map))

    def test_source_metadata_is_not_used(self):
        image_map = json.loads((self.path / "map.json").read_text())
        image_map.pop("symbols")
        image_map["packets"] = [{"text": "not assembly", "word_address": 0, "word_count": 999}]
        (self.path / "map.json").write_text(json.dumps(image_map))
        self.assertEqual(Cpu(Image.load(self.path / "regions.json")).run().output, b"Hello, world!\n")

    def test_corrupt_binary_rejected(self):
        path = self.path / "text.bin"
        path.write_bytes(bytes(len(path.read_bytes())))
        with self.assertRaisesRegex(CpuError, "hash mismatch"):
            Image.load(self.path)

    def test_missing_end_bit_rejected(self):
        path = self.path / "text.bin"
        raw = bytearray(path.read_bytes())
        raw[-1] &= 0x7F
        path.write_bytes(raw)
        self.change_manifest(lambda regions: regions[0].update(sha256=hashlib.sha256(raw).hexdigest()))
        with self.assertRaisesRegex(CpuError, "unterminated"):
            Image.load(self.path)

    def test_manifest_cannot_escape_directory(self):
        self.change_manifest(lambda regions: regions[0].update(file="../outside.bin"))
        with self.assertRaisesRegex(CpuError, "inside the image directory"):
            Image.load(self.path)

    def test_overlapping_regions_rejected(self):
        self.change_manifest(lambda regions: regions.append(dict(regions[0])))
        with self.assertRaisesRegex(CpuError, "overlapping"):
            Image.load(self.path)

    def test_bad_entry_rejected(self):
        image_map = json.loads((self.path / "map.json").read_text())
        image_map["entry_word_address"] += 1
        (self.path / "map.json").write_text(json.dumps(image_map))
        with self.assertRaisesRegex(CpuError, "entry"):
            Image.load(self.path)

    def test_cli_demo_image_and_error_exit_codes(self):
        for arguments in ([], [str(self.path)]):
            result = subprocess.run([sys.executable, str(HERE / "run.py"), *arguments],
                                    cwd=self.temporary.name, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"Hello, world!\n")
        result = subprocess.run([sys.executable, str(HERE / "run.py"), str(self.path), "--max-packets", "1"],
                                capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"packet limit", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
