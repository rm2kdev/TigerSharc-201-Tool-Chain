"""TS201 word-addressed assembler. Encoding authority: ADI PR Rev 1.1, Appendix C.

No vendor programs, firmware words, or third-party packages.
See docs/ENCODINGS.md for independently reviewable field derivations.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import struct


class AssemblyError(ValueError):
    pass


# Half-open DSP WORD ranges from the six physical internal memory banks.
BANKS = tuple((base, base + 0x20000) for base in
              (0, 0x40000, 0x80000, 0xC0000, 0x100000, 0x140000))
EX = 1 << 31
NOP = 0x33C00000
REG = r"([JK](?:[0-9]|[12][0-9]|3[01]))"
IDENT = r"[A-Za-z_][A-Za-z_0-9]*"


def require(ok, message):
    if not ok:
        raise AssemblyError(message)


def expression(text: str, symbols: dict[str, int] | None = None) -> int:
    """Integer literals, labels, ADDRESS(label), unary + - ~ and the binary
    operators + - * / % << >> & | ^ (C semantics: / and % truncate toward
    zero). Never eval. PR 7-40/7-45/8-93/A-16 examples use these forms."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise AssemblyError(f"invalid expression: {text!r}") from None

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.Name):
            require(not re.fullmatch(r"(?:[JK]|[XY]{0,2}[FLSBQ]?R)\d+", node.id, re.I),
                    f"register {node.id} cannot be an expression operand")
            require(symbols is not None and node.id in symbols,
                    f"undefined symbol (or non-literal layout value): {node.id}")
            return symbols[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
            return ~visit(node.operand)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.upper() == "ADDRESS"
                and len(node.args) == 1 and not node.keywords):
            # Symbols are DSP word addresses already; ADDRESS(x) is x (8-93).
            return visit(node.args[0])
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            op = node.op
            if isinstance(op, ast.Add): return left + right
            if isinstance(op, ast.Sub): return left - right
            if isinstance(op, ast.Mult): return left * right
            if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
                require(right != 0, f"division by zero in expression: {text!r}")
                quotient = abs(left) // abs(right) * (1 if (left < 0) == (right < 0) else -1)
                return quotient if not isinstance(op, ast.Mod) else left - quotient * right
            if isinstance(op, (ast.LShift, ast.RShift)):
                require(0 <= right < 64, f"shift count out of range in expression: {text!r}")
                return left << right if isinstance(op, ast.LShift) else left >> right
            if isinstance(op, ast.BitAnd): return left & right
            if isinstance(op, ast.BitOr): return left | right
            if isinstance(op, ast.BitXor): return left ^ right
        raise AssemblyError(f"unsupported expression: {text!r}")

    try:
        return visit(tree.body)
    except RecursionError:
        raise AssemblyError("expression too deeply nested") from None


def has_symbol(text):
    # Validate syntax even in the layout pass; unresolved names are allowed here.
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise AssemblyError(f"invalid expression: {text!r}") from None
    names = {n.id: 1 for n in ast.walk(tree) if isinstance(n, ast.Name)
             and not any(isinstance(c, ast.Call) and c.func is n for c in ast.walk(tree))}
    try:
        expression(text, names)
    except AssemblyError as error:
        if "division by zero" not in str(error) and "shift count" not in str(error):
            raise
    return bool(names)


def word_value(value):
    require(-(1 << 31) <= value <= 0xFFFFFFFF, "value does not fit a 32-bit word")
    return value & 0xFFFFFFFF


def signed_word(value):
    value = word_value(value)
    return value if value < 0x80000000 else value - (1 << 32)


def reg(text, *, ureg=False, dest=False):
    m = re.fullmatch(REG, text.strip(), re.I)
    require(m is not None, f"unsupported register: {text}")
    name = m[1].upper()
    unit, index = int(name[0] == "K"), int(name[1:])
    require(index != 31 or not ureg,
            "use JSTAT/KSTAT for scalar status-register transfers")
    return unit, index


def operand(text, unit):
    if re.fullmatch(REG, text, re.I):
        other, index = reg(text)
        require(other == unit, "mixed J/K operands in one IALU operation")
        return index << 1
    value = expression(text)
    require(-128 <= value <= 127, "arithmetic/address immediate must be signed 8-bit (-128..127)")
    return (1 << 13) | (value & 255)


@dataclass
class Instruction:
    kind: str
    words: list[int]
    unit: int | None = None
    target: str | None = None
    absolute: bool = False
    resources: frozenset = frozenset()
    writes: frozenset = frozenset()
    predicted: bool = False
    two_buses: bool = False
    exclusive: bool = False
    nf_ok: bool = True  # form accepts (NF) (NF notes PR 3-13, 4-43, 5-22, 6-19, 7-11)


def instruction(text, symbols=None, pc=0, layout=False):
    text = text.strip()
    from .compute import encode as compute_encode
    from .control import encode as control_encode
    from .transfers import encode as transfer_encode
    from .ialu import encode as ialu_encode
    from .compute import UNPREFIXED
    compute_error = None
    # PR 2-7: "XY (or no prefix) = both" compute blocks. A failed compute
    # reading falls through to Ureg transfers ('XR3:0 = R7:4', PR 1-45).
    for candidate in [text] + (["XY " + text] if UNPREFIXED.match(text) else []):
        try:
            result = compute_encode(candidate)
        except AssemblyError as error:
            compute_error = compute_error or error
            continue
        if result is not None:
            return result
    for encoder in (control_encode, ialu_encode, transfer_encode):
        try:
            result = encoder(text, symbols, pc, layout)
        except AssemblyError:
            # A memory operand makes the transfer reading the relevant one.
            if compute_error is None or "[" in text:
                raise
            result = None
        if result is not None:
            return result
    if compute_error is not None:
        raise compute_error
    raise AssemblyError(f"unsupported instruction or options: {text!r}")


def packet(text, symbols=None, pc=0, layout=False):
    from .control import condition
    from .packet_rules import validate, SEQUENCER, NF_HEADERS
    parts = [s.strip() for s in text.split(";")]
    require(all(parts), "empty operation in packet")
    comma_do = re.fullmatch(r'(IF\s+\w+\s*(?:\(NF\))?)\s*,\s*(DO\b.*)', parts[0], re.I)
    if comma_do:
        # Examples on 3-16, 5-23, 6-20 and 7-14 write 'IF XSF0, DO ...'.
        parts[0:1] = [comma_do[1], comma_do[2]]
    conditional = re.fullmatch(r'IF\s+(\w+)\s*(\(NF\))?', parts[0], re.I)
    marker = None
    ops = []
    controlled = []
    if conditional:
        code, negate = condition(conditional[1])
        first = 0x33000000 | (1-negate)<<22 | code<<16 | int(bool(conditional[2]))<<15
        ops.append(Instruction('condition',[first]))
        controlled.append(False)
        marker = 'DO'
        require(len(parts)>1, 'IF requires at least one operation')
        parts = parts[1:]
    for i, part in enumerate(parts):
        prefix = re.match(r'(DO|ELSE)\b\s*,?\s*', part, re.I)
        if prefix:
            require(marker==prefix[1].upper(), 'DO/ELSE requires the corresponding conditional packet header')
            part=part[prefix.end():]
        op = instruction(part, symbols, pc, layout)
        if not ops and op.kind in ('branch','indirect'):
            marker='ELSE'
        elif marker:
            # NOP..EMUTRAP may not be conditional (PR 8-99); an unmarked NOP
            # executes regardless of the condition and may share the line.
            require(op.kind in ('alu','compute','memory','move','constant') or op.kind=='nop' and not prefix,
                    'conditional body requires compute, IALU or data-transfer operations')
            if marker=='ELSE':
                require(not (op.kind=='alu' and ((op.words[0]>>21)&31) in (2,3)),
                        'branch packet cannot also write CJMP')
        ops.append(op)
        controlled.append(bool(prefix))
    branch = ops[0] if ops[0].kind == 'branch' and len(ops[0].words) == 2 else None
    if branch and (sum(len(op.words) for op in ops) > 4 or any(len(op.words) > 1 for op in ops[1:])):
        # The assembler owns the extension slot (PR 1-23, 7-42). When the
        # 32-bit form cannot fit, use the one-slot 15-bit form (PR 1-35
        # table 1-2, C-39): C-42 'if <cond1>, jump <label 1>; nop; nop; nop;;',
        # 8-95 'If NJEQ, JUMP _outer_loop; NOP ; NOP ; NOP ;;'. Decided on word
        # counts only, so layout and final passes agree.
        first, extension = branch.words
        value = (extension & 0x1FFFF) << 15 | first & 0x7FFF
        if branch.absolute:
            require(layout or value < 0x8000, 'one-slot ABS branch target must be below 0x8000 (PR 10-234)')
        else:
            require(layout or -0x4000 <= signed_word(value) < 0x4000,
                    'branch needs its immediate extension but the packet has no free slot; target is outside the one-slot 15-bit range')
        branch.words = [first]
    require(sum(len(op.words) for op in ops)<=4, 'packet exceeds four encoded instruction words')
    if ops[0].kind in NF_HEADERS:
        # Bit 30 of the other slots is CC here, so per-slot (NF) becomes the
        # header's line NF (bit 15). PR A-14 note 1 / 8-16: legal only when
        # every slot that supports NF uses it; COMP, PASS and other NF-less
        # forms may share the line.
        nf_ops=[op for op in ops[1:] if op.words[0]&(1<<30)]
        if nf_ops:
            capable=[op for op in ops[1:] if op.kind in ('alu','compute') and op.nf_ok]
            require(all(op.words[0]&(1<<30) for op in capable),
                    '(NF) on a sequencer-headed line must be on every slot that supports it (PR A-14)')
            for op in nf_ops:
                op.words[0]&=~(1<<30)
            ops[0].words[0]|=1<<15
    validate(ops)
    extensions=[w for op in ops for w in op.words[1:]]
    require(len(extensions)<=1, 'only one immediate extension per packet')
    # PR 1-23 Figure 1-6: the assembler puts the extension in slot 2; "the
    # instruction using the extension may use any other slot" (the extension
    # carries its J/K unit, C-37). 7-42's "first slot" is programming advice.
    # The condition's NF bit controls every operation; each DO/ELSE marker
    # independently sets CC on that operation. Unmarked operations always run.
    words=[]
    for i,(op,cc) in enumerate(zip(ops,controlled)):
        words.append(op.words[0] | (int(cc)<<30))
        if i==0: words.extend(extensions)
    require(len(words)<=4, 'packet exceeds four encoded instruction words')
    words[-1] |= EX
    return words, ops


@dataclass
class Section:
    name: str
    kind: str
    start_word_address: int
    words: list[int] = field(default_factory=list)


@dataclass
class Assembly:
    sections: list[Section]
    symbols: dict[str, int]
    entry_word_address: int
    packets: list[dict]

    def write(self, directory):
        directory = Path(directory)
        # A build creates a new owned directory; never clobber an existing image.
        require(not directory.exists(), f"output directory already exists: {directory}; choose a fresh directory")
        directory.mkdir(parents=True)
        regions = []
        for section in self.sections:
            raw = struct.pack(f"<{len(section.words)}I", *section.words)
            filename = section.name + ".bin"
            (directory / filename).write_bytes(raw)
            regions.append(dict(file=filename, name=section.name, kind=section.kind,
                start_word_address=f"0x{section.start_word_address:08x}",
                end_word_address_exclusive=f"0x{section.start_word_address + len(section.words):08x}",
                words=len(section.words), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        image_map = dict(format="ts201-map-v1", address_unit="32-bit DSP word",
            byte_order="little", entry_word_address=self.entry_word_address,
            symbols=dict(sorted(self.symbols.items())), packets=self.packets, regions=regions)
        for name, data in (("regions.json", regions), ("map.json", image_map)):
            (directory / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        lines = ["TS201 map v1: addresses in DSP words; file offsets in bytes", f"ENTRY 0x{self.entry_word_address:08x}"]
        for s in self.sections:
            lines.append(f"{s.name:20} {s.kind:4} 0x{s.start_word_address:08x} words={len(s.words)} bytes={4*len(s.words)}")
        lines += [f"0x{address:08x} {name}" for name, address in sorted(self.symbols.items(), key=lambda pair: (pair[1], pair[0]))]
        (directory / "symbols.map").write_text("\n".join(lines) + "\n", encoding="utf-8")


def strip_comments(source):
    # Preserve physical line numbers. Unclosed block comments must not hide errors.
    pattern = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
    result = pattern.sub(lambda m: "\n" * m[0].count("\n") if m[0].startswith("/*") else "", source)
    require("/*" not in result or "*/" in result, "unterminated block comment")
    return result


def assemble(source: str, filename="<source>", *, memory_windows=None) -> Assembly:
    windows = BANKS if memory_windows is None else tuple(memory_windows)
    symbols, sections, records, packets = {}, [], [], []
    current = None
    entry = None
    entry_line = 0
    section_names = set()
    line_number = 0
    try:
        for line_number, raw in enumerate(strip_comments(source).splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            label = re.match(r"(" + IDENT + r")\s*:(?![0-9])\s*", line)
            if label:
                require(current is not None, "label before a section")
                require(label[1] not in symbols, f"duplicate symbol: {label[1]}")
                require(not re.fullmatch(REG, label[1], re.I), "register name cannot be a label")
                from .registers import REGISTERS
                require(label[1].upper() not in REGISTERS and not re.fullmatch(
                    r'(?:(?:XY|YX|X|Y)?[FLSBQ]?R\d+|(?:[SL]?MR|THR|TR|PR)\d+|CMCTL|(?:X|Y)?STATL?|BFOTMP)',label[1],re.I),
                    'register name cannot be a label')
                symbols[label[1]] = current.start_word_address + len(current.words)
                line = line[label.end():].strip()
                if not line:
                    continue
            if line.startswith(".section "):
                parts = [p.strip() for p in line[9:].split(",")]
                require(len(parts) == 3, ".section requires name, code|data, literal word address")
                name, kind, base = parts
                require(re.fullmatch(IDENT, name) is not None and name.lower() not in section_names, "invalid or duplicate section name")
                require(not re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", name, re.I), "section name is reserved on Windows")
                require(kind in ("code", "data"), "section kind must be code or data")
                address = expression(base)
                require(any(lo <= address < hi for lo, hi in windows), "section address is outside TS201 internal RAM (reserved gap or external memory)")
                current = Section(name, kind, address)
                sections.append(current)
                section_names.add(name.lower())
                continue
            if line.startswith(".entry "):
                require(entry is None, "duplicate .entry")
                entry, entry_line = line[7:].strip(), line_number
                has_symbol(entry)
                continue
            require(current is not None, "operation before a section")
            at = current.start_word_address + len(current.words)
            if line.startswith(".align "):
                alignment = expression(line[7:])
                require(1 <= alignment <= 0x20000 and alignment & (alignment - 1) == 0, "alignment must be a power of two in WORDS (1..131072)")
                count = (-at) % alignment
                if current.kind == "code":
                    for i in range(count):
                        packets.append(dict(word_address=at + i, word_count=1, line=line_number, text="NOP [alignment]"))
                current.words += [NOP | EX if current.kind == "code" else 0] * count
            elif line.startswith(".word "):
                require(current.kind == "data", ".word is data-only; raw instruction replay is unsupported")
                values = [v.strip() for v in line[6:].split(",")]
                require(all(values), "empty .word operand")
                for value in values:
                    has_symbol(value)
                records.append((current, len(current.words), "data", values, line_number))
                current.words += [0] * len(values)
            elif line.startswith(".zero "):
                require(current.kind == "data", ".zero is data-only")
                count = expression(line[6:])
                require(1 <= count <= 0x20000, ".zero count must be 1..131072 words")
                current.words += [0] * count
            else:
                require(current.kind == "code", "instructions require a code section")
                require(line.endswith(";;") and ";;" not in line[:-2], "exactly one complete packet per physical line, ending with ;;")
                text = line[:-2].strip()
                words, _ = packet(text, layout=True)
                records.append((current, len(current.words), "code", text, line_number))
                current.words += words
                packets.append(dict(word_address=at, word_count=len(words), line=line_number, text=text))
            require(any(lo <= current.start_word_address and current.start_word_address + len(current.words) <= hi for lo, hi in windows), "section crosses internal bank boundary into reserved memory")
        require(sections, "no sections")
        for s in sections:
            require(s.words, f"empty section: {s.name}")
        ordered = sorted(sections, key=lambda s: s.start_word_address)
        for a, b in zip(ordered, ordered[1:]):
            require(a.start_word_address + len(a.words) <= b.start_word_address, "overlapping sections")
        starts = {p["word_address"] for p in packets}
        predicted_quads = set()
        for s, offset, kind, value, line_number in records:
            if kind == "data":
                s.words[offset:offset + len(value)] = [word_value(expression(v, symbols)) for v in value]
            else:
                words, ops = packet(value, symbols, s.start_word_address + offset)
                for op in ops:
                    if op.predicted:
                        quad = (s.start_word_address + offset + len(words) - 1) // 4
                        require(quad not in predicted_quads, 'two predicted branches end in the same aligned quad; use NP or alignment')
                        predicted_quads.add(quad)
                    if op.target is not None:
                        require(expression(op.target, symbols) in starts, "branch target must be a code packet start (not data, gap, or extension word)")
                s.words[offset:offset + len(words)] = words
        line_number = entry_line
        require(entry is not None, "missing .entry")
        address = expression(entry, symbols)
        require(address in starts, "entry must be a code packet start")
        return Assembly(sections, symbols, address, sorted(packets, key=lambda p: p["word_address"]))
    except AssemblyError as error:
        raise AssemblyError(f"{filename}:{line_number}: {error}") from None
