"""A small functional TS201 integer subset; no timing or peripheral model."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct

MASK = 0xFFFFFFFF
EX = 0x80000000
BANKS = tuple((base, base + 0x20000) for base in
              (0, 0x40000, 0x80000, 0xC0000, 0x100000, 0x140000))
# A host console convention, not a processor register or physical peripheral.
CONSOLE_WORD = 0xFFFF0000


class CpuError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CpuError(message)


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def in_ram(address):
    return any(low <= address < high for low, high in BANKS)


@dataclass
class Image:
    memory: dict[int, int]
    executable: set[int]
    starts: set[int]
    entry: int

    @classmethod
    def load(cls, path):
        """Load and validate assembler .bin sections and their JSON manifests.

        Instruction boundaries come from EX bits in binary words. Source text,
        symbol names and the map's packet descriptions are never interpreted.
        """
        path = Path(path)
        manifest = path / "regions.json" if path.is_dir() else path
        directory = manifest.resolve().parent
        try:
            regions = json.loads(manifest.read_text(encoding="utf-8"))
            image_map = json.loads((directory / "map.json").read_text(encoding="utf-8"))
            require(image_map["format"] == "ts201-map-v1", "unsupported image map format")
            require(image_map["byte_order"] == "little", "image must be little-endian")
            require(image_map["address_unit"] == "32-bit DSP word", "image must use DSP word addresses")
            require(isinstance(regions, list) and regions, "empty or invalid region manifest")
            require(image_map["regions"] == regions, "region manifests disagree")
            memory, executable, starts = {}, set(), set()
            for region in regions:
                name = region["file"]
                require(isinstance(name, str) and name, "invalid region filename")
                binary = (directory / name).resolve()
                require(binary.parent == directory, "region file must be inside the image directory")
                start = int(region["start_word_address"], 0)
                count = region["words"]
                require(type(count) is int and count >= 0, "invalid region word count")
                require(any(low <= start < high and start + count <= high for low, high in BANKS),
                        "region outside a TS201 internal RAM bank")
                require(int(region["end_word_address_exclusive"], 0) == start + count,
                        "region end address mismatch")
                require(region["kind"] in ("code", "data"), "invalid region kind")
                raw = binary.read_bytes()
                require(len(raw) == region["bytes"] == count * 4, "region byte count mismatch")
                require(hashlib.sha256(raw).hexdigest() == region["sha256"], "region hash mismatch")
                words = struct.unpack(f"<{count}I", raw)
                pending = 0
                for offset, word in enumerate(words):
                    address = start + offset
                    require(address not in memory, "overlapping image regions")
                    memory[address] = word
                    if region["kind"] == "code":
                        executable.add(address)
                        if pending == 0:
                            starts.add(address)
                        pending += 1
                        require(pending <= 4, "instruction packet exceeds four words")
                        if word & EX:
                            pending = 0
                require(pending == 0, "unterminated instruction packet")
            entry = image_map["entry_word_address"]
            require(type(entry) is int and entry in starts, "entry is not a loaded packet start")
            return cls(memory, executable, starts, entry)
        except CpuError:
            raise
        except (OSError, ValueError, TypeError, KeyError, struct.error) as error:
            raise CpuError(f"invalid image: {error}") from error


class Cpu:
    def __init__(self, image):
        self.memory = dict(image.memory)
        self.executable = set(image.executable)
        self.starts = set(image.starts)
        self.pc = image.entry
        self.registers = [[0] * 31, [0] * 31]  # J0..30 and K0..30
        self.zero = [False, False]
        self.output = bytearray()
        self.halted = False
        self.packets = 0

    def reg(self, unit, index):
        # J31/K31 are zero in IALU and address expressions (PR 7-4).
        return 0 if index == 31 else self.registers[unit][index]

    def set_reg(self, unit, index, value):
        if index != 31:
            self.registers[unit][index] = value & MASK

    def transfer_reg(self, group, index):
        require(group in (12, 13) and index < 31,
                "only scalar J0..30/K0..30 transfers are supported")
        return group - 12, index

    def read(self, address):
        require(in_ram(address), f"invalid RAM read at word 0x{address:08x}")
        return self.memory.get(address, 0)

    def write(self, address, value):
        if address == CONSOLE_WORD:
            self.output.append(value & 0xFF)
        else:
            require(in_ram(address), f"invalid RAM write at word 0x{address:08x}")
            require(address not in self.executable, "self-modifying code is not supported")
            self.memory[address] = value & MASK

    def _immediate(self, word, extension, unit, bits):
        if extension is None:
            return signed(word & ((1 << bits) - 1), bits) & MASK
        require(extension & 0x3F000000 == 0x32000000 and (extension >> 30) == unit,
                "unsupported parallel packet or wrong IALU extension")
        if bits == 15:
            require((word >> 8) & 0x7F == extension & 0x7F, "constant extension overlap mismatch")
        return ((extension & 0xFFFFFF) << 8) | (word & 0xFF)

    def _operand(self, word, extension, unit):
        if word & 0x2000:
            return self._immediate(word, extension, unit, 8)
        require(extension is None and word & 0xC1 == 0,
                "invalid register operand or unsupported parallel packet")
        return self.reg(unit, (word >> 1) & 31)

    def step(self):
        require(not self.halted, "CPU is halted")
        require(self.pc in self.starts, f"PC 0x{self.pc:08x} is not a loaded packet start")
        words = []
        for offset in range(4):
            address = self.pc + offset
            require(address in self.executable, "instruction fetch outside loaded code")
            raw = self.memory[address]
            words.append(raw & ~EX)
            if raw & EX:
                break
        else:
            raise CpuError("unterminated instruction packet")
        require(len(words) <= 2, "parallel instruction packets are not supported")
        word = words[0]
        extension = words[1] if len(words) == 2 else None
        next_pc = (self.pc + len(words)) & MASK
        unit = (word >> 28) & 1
        try:
            if word in (0x33C00000, 0x33C40000):  # NOP, IDLE
                require(extension is None, "parallel instruction packets are not supported")
                self.halted = word == 0x33C40000
            elif word & 0x3E000000 == 0x30000000:  # Direct branch, C-21.
                require(not word & 0x800000, "CALL is not supported")
                condition = (word >> 16) & 63
                require(condition in (0, 4, 8), "only TRUE, JEQ and KEQ branch conditions are supported")
                take = True if condition == 8 else self.zero[condition // 4]
                if word & 0x400000:
                    take = not take
                relative = bool(word & 0x1000000)
                if extension is not None:
                    require(extension & 0x7FFE0000 == 0x33800000,
                            "unsupported parallel packet or invalid branch extension")
                    target = ((extension & 0x1FFFF) << 15) | (word & 0x7FFF)
                else:
                    target = signed(word & 0x7FFF, 15) if relative else word & 0x7FFF
                if relative:
                    target = (self.pc + target) & MASK
                if take:
                    require(target in self.starts, f"branch target 0x{target:08x} is not a packet start")
                    next_pc = target
            elif word & 0x2C000000 == 0x0C000000:  # Integer ALU, C-13/C-14.
                operation = (word >> 21) & 31
                dest = (word >> 16) & 31
                require(operation in (0, 1, 12, 13), "only ADD, SUB and COMP IALU operations are supported")
                require(not word & 0xC000, "reserved IALU bits are set")
                left = self.reg(unit, (word >> 8) & 31)
                right = self._operand(word, extension, unit)
                if operation in (12, 13):
                    require(dest == 0 and not word & 0x40000000, "invalid COMP encoding")
                    self.zero[unit] = left == right
                else:
                    value = (left + right if operation == 0 else left - right) & MASK
                    self.set_reg(unit, dest, value)
                    if not word & 0x40000000:
                        self.zero[unit] = value == 0
            elif word & 0x2C008000 == 0x08008000:  # Scalar constant, C-16.
                require(not word & 0x40000000, "conditional transfers are not supported")
                bank, index = self.transfer_reg((word >> 21) & 31, (word >> 16) & 31)
                require(unit == bank, "constant uses the wrong IALU unit")
                self.set_reg(bank, index, self._immediate(word, extension, unit, 15))
            elif word & 0x28000000 == 0:  # Scalar memory transfer, C-17.
                require(not word & 0x40004000, "conditional or wide transfers are not supported")
                bank, index = self.transfer_reg((word >> 21) & 31, (word >> 16) & 31)
                base = (word >> 8) & 31
                post = bool(word & 0x8000)
                store = bool(word & 0x4000000)
                require(not post or base != 31, "cannot post-modify J31/K31")
                require(store or not post or (unit, base) != (bank, index),
                        "post-modify load writes the same register twice")
                offset = self._operand(word, extension, unit)
                updated = (self.reg(unit, base) + offset) & MASK
                address = self.reg(unit, base) if post else updated
                if store:
                    self.write(address, self.reg(bank, index))
                else:
                    self.set_reg(bank, index, self.read(address))
                if post:
                    self.set_reg(unit, base, updated)
            else:
                raise CpuError(f"unsupported instruction 0x{word:08x}")
        except CpuError as error:
            raise CpuError(f"PC 0x{self.pc:08x}: {error}") from error
        self.pc = next_pc
        self.packets += 1

    def run(self, max_packets=100_000):
        require(type(max_packets) is int and max_packets > 0, "packet limit must be positive")
        for _ in range(max_packets):
            if self.halted:
                return self
            self.step()
        require(self.halted, f"packet limit ({max_packets}) exceeded at PC 0x{self.pc:08x}")
        return self
