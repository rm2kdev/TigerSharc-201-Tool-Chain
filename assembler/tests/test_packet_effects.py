import unittest
from ts201.assembler import packet
from ts201.packet_rules import effects


def op(text):
    return packet(text)[1][0]


J = lambda i: ('ureg', 12 * 32 + i)
K = lambda i: ('ureg', 13 * 32 + i)


class EffectsTests(unittest.TestCase):
    def test_ialu_arithmetic(self):
        reads, writes, touches = effects(op('J1 = J2 + J3'))
        self.assertEqual(reads, {J(2), J(3), J(31)})
        self.assertEqual(writes, {J(1), J(31)})
        self.assertEqual(touches, [])
        reads, writes, _ = effects(op('K4 = K5 - 7 (NF)'))
        self.assertEqual((reads, writes), ({K(5), K(31)}, {K(4), K(31)}))
        reads, writes, _ = effects(op('COMP(J2, J3)'))
        self.assertEqual((reads, writes), ({J(2), J(3), J(31)}, {J(31)}))
        self.assertIn(('ureg', 26 * 32), effects(op('J0 = J0 + 0 (CJMP)'))[1])

    def test_memory(self):
        reads, writes, touches = effects(op('J6 = [J27 + 2]'))
        self.assertEqual((reads, writes, touches), ({J(27)}, {J(6)}, [(J(27), 2, 1, False)]))
        reads, writes, touches = effects(op('Q [J27 + 4] = XR3:0'))
        self.assertEqual(reads, {J(27)} | {(1, i) for i in range(4)})
        self.assertEqual((writes, touches), (set(), [(J(27), 4, 4, True)]))
        # Post-modify addresses the old pointer and writes the pointer.
        reads, writes, touches = effects(op('XR4 = [K11 += 1]'))
        self.assertEqual((writes, touches), ({(1, 4), K(11)}, [(K(11), 0, 1, False)]))
        # Register offsets, extensions and J31 absolute forms.
        self.assertEqual(effects(op('XR0 = [J1 + J2]'))[2], [(J(1), None, 1, False)])
        self.assertEqual(effects(op('XR0 = [J1 + 0x1000]'))[2], [(J(1), None, 1, False)])
        reads, _, touches = effects(op('XR0 = [J31 + 5]'))
        self.assertEqual((reads, touches), (set(), [(('zero',), 5, 1, False)]))

    def test_transfers_and_compute(self):
        self.assertEqual(effects(op('XR3 = J2')), ({J(2)}, {(1, 3)}, []))
        self.assertEqual(effects(op('K1 = 0x12345')), (set(), {K(1)}, []))
        reads, writes, _ = effects(op('X R3 = R1 + R2'))
        self.assertIn((1, 17), reads)
        self.assertIn(('state', 1), writes)
        self.assertNotIn((2, 3), writes)
        self.assertEqual(effects(op('CALL 0 (ABS)'))[1], {('ureg', 26 * 32)})
        self.assertEqual(effects(op('JUMP 0 (ABS)'))[1], set())
        self.assertIsNone(effects(op('CJMP (ABS)')))


if __name__ == '__main__':
    unittest.main()
