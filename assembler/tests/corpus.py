"""Generate original programs and host-integer expectations independently of encoders."""


def integer_corpus():
    lines = [".entry start", ".section text, code, 0x1001", "start:", "J10 = results;;", "K10 = results;;"]
    expected = []
    pairs = [(0, 0), (1, 2), (0xFFFFFFFF, 1), (0x7FFFFFFF, 1),
             (0x80000000, 1), (0x80000000, 0xFFFFFFFF), (0x1234ABCD, 0xFFFF80FF),
             (0x7FFFFFFF, 0x80000000)]
    operations = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
                  "AND": lambda a, b: a & b, "OR": lambda a, b: a | b, "XOR": lambda a, b: a ^ b}
    for unit in ("J", "K"):
        for a, b in pairs:
            for op, calculate in operations.items():
                # Each bank's pointer is initialized independently to the right offset.
                lines += [f"{unit}10 = results + {len(expected)};;", f"{unit}0 = {a};;", f"{unit}1 = {b};;",
                          f"{unit}2 = {unit}0 {op} {unit}1;;", f"[{unit}10 + 0] = {unit}2;;"]
                expected.append(calculate(a, b) & 0xFFFFFFFF)
    lines += ["done: IDLE;;", ".section results_data, data, 0xC0000", f"results: .zero {len(expected)}"]
    return "\n".join(lines), {"max_packets": 1000, "registers": {}, "memory": {"results": expected}, "idle_symbol": "done"}


def condition_corpus():
    lines = [".entry start", ".section text, code, 0x2001", "start:", "J10 = results;;"]
    expected = []
    for unit in ("J", "K"):
        for unsigned in (False, True):
            for a, b in [(5, 5), (0xFFFFFFFF, 1), (1, 0xFFFFFFFF)]:
                signed = lambda v: v if v < 0x80000000 else v - (1 << 32)
                aa, bb = (a, b) if unsigned else (signed(a), signed(b))
                for suffix, answer in (("EQ", aa == bb), ("LT", aa < bb), ("LE", aa <= bb)):
                    for negate in (False, True):
                        n = len(expected)
                        condition = ("N" if negate else "") + unit + suffix
                        lines += [f"{unit}0 = {a};;", f"{unit}1 = {b};;",
                                  f"COMP({unit}0, {unit}1)" + (" (U)" if unsigned else "") + ";;",
                                  f"IF {condition}, JUMP yes_{n} (NP);;", "J3 = 0;;",
                                  f"JUMP store_{n} (NP);;", f"yes_{n}: J3 = 1;;",
                                  f"store_{n}: [J10 += 1] = J3;;"]
                        expected.append(int(answer != negate))
    # NF must preserve the less-than flag across an arithmetic instruction.
    lines += ["J0 = -1;;", "COMP(J0, 0);;", "J0 = J31 + 9 (NF);;",
              "IF JLT, JUMP done (NP);;", "bad: IDLE;;", "done: IDLE;;",
              ".section results_data, data, 0x40000", f"results: .zero {len(expected)}"]
    return "\n".join(lines), {"max_packets": 1000, "registers": {"J0": 9}, "memory": {"results": expected}, "idle_symbol": "done"}
