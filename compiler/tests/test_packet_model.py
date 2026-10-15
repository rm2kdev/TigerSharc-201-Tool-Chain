"""The scheduler's packet model (backend.fits) must never accept a packet the
assembler rejects: backend.schedule_region silently splits such packets into
one instruction per line, which costs packets without failing any test."""
import random
import unittest
from ts201cc import backend
from ts201cc.backend import classify, fits, packet_text, packet_ok
from ts201cc.isel import MI


class _Label:
    label = '_cc_b0_1'


def vocabulary():
    X = [f'XR{i}' for i in (0, 1, 5, 9, 13, 17)]
    Y = [f'YR{i}' for i in (0, 3, 6)]
    J = [f'J{i}' for i in (0, 2, 5, 9, 14)]
    K = [f'K{i}' for i in (0, 3, 6, 10)]
    out = []
    for d in J + K:
        for u in J + K:
            if d[0] != u[0]: continue
            for imm in (1, 127, -128, 128, -129, 1280):
                out.append(MI('ialu', [d], [u], imm, '+'))
                out.append(MI('ialu', [d], [u], imm, '-'))
    for d in X + Y + J + K:
        for imm in (0, 32, 20000):
            out.append(MI('li', [d], [], imm))
            out.append(MI('cset', [d], [], imm, 'XALT', extra='X'))
    for d in X + Y + J + K:
        for base in J + K:
            for imm in (0, 13, 200):
                out.append(MI('ld', [d], [base], imm))
                out.append(MI('st', [], [base, d], imm))
            out.append(MI('ld', [d], [base, base[0] + '1']))
    for d in X + Y:
        for s in J + K:
            out.append(MI('mov', [d], [s])); out.append(MI('mov', [s], [d]))
    for c in ('XR', 'YR'):
        out.append(MI('xalu', [c + '4'], [c + '1', c + '2'], None, '+'))
        out.append(MI('xcmp', [], [c + '1', c + '2']))
    for cond in ('NLC0E', 'XALT', 'JEQ'):
        out.append(MI('br', [], [], None, cond, t=[_Label]))
    out.append(MI('jmp', t=[_Label]))
    return out


class PacketModelTests(unittest.TestCase):
    def test_accepted_packets_assemble(self):
        rng = random.Random(201)
        vocab = vocabulary()
        bad = []
        for _ in range(6000):
            packet = []
            for mi in rng.sample(vocab, 6):
                n = classify(mi)
                if fits(packet, n): packet.append(n)
            text = packet_text(packet)
            if not packet_ok(text): bad.append(text)
        self.assertEqual(bad[:10], [])

    def test_known_mismatches(self):
        cross = classify(MI('ld', ['K1'], ['J9'], 13))
        self.assertFalse(fits([classify(MI('cset', ['XR9'], [], 32, 'XALT', extra='X'))], cross))
        a = classify(MI('ialu', ['K2'], ['K0'], 1, '+'))
        b = classify(MI('ialu', ['J4'], ['J3'], -128, '+'))
        self.assertTrue(packet_ok(packet_text([a, b])))


if __name__ == '__main__':
    unittest.main()
