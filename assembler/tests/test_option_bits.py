"""Independent option-bit worksheet over tests/appendix_a_options.txt.

For every corpus statement carrying options, encode(with) XOR encode(without)
must equal the mask read from PR Rev 1.1 Appendix C. Masks are entered here
by hand from the tables, not taken from the encoder:
C-5..C-7 (Table C-3 footnotes 3..6), C-7..C-10 (Table C-4 footnotes 1,3,5),
C-10..C-12 (Table C-5 footnotes 1,3), C-15..C-18 (Table C-7 and the XY/U/I/
S/T/C/R/ab bit list), C-20..C-21 (Tables C-8/C-9), C-25 (Table C-12),
C-6 (Table C-6 TMAX bit 10), C-28 (Table C-14), C-1 (NF bit 30).
"""
from pathlib import Path
import re
import unittest

from ts201.assembler import AssemblyError, packet

OP = 15  # compute opcode field, bits 19:15 (Fig C-2)
RULES = [  # first matching body regex wins
    (r'\+= FDEP', {'SE': 1 << OP, 'ZF': 2 << OP}),                         # C-20/21
    (r'^MR[0-9:]+ = R', {'SE': 1 << 10, 'ZE': 1 << 10 | 16 << OP}),       # C-17 Rs 01000 -> 01001, U
    (r'^[SL]MR[0-9:]+ = R', {'SE': 0, 'ZE': 16 << OP}),                   # C-17 U (omitted = SE)
    (r'= COMPACT MR', {'U': 16 << OP, 'I': 8 << OP, 'S': 4 << OP}),        # C-18
    (r'R[0-9:]+ = S?MR[0-9:]+$', {'U': 16 << OP, 'S': 4 << OP}),          # C-18
    (r'R[0-9:]+ = L?MR[0-9]$', {'U': 16 << OP}),                           # C-18
    (r'^MR.*\*\*', {'I': 8 << OP, 'C': 4 << OP, 'CR': 6 << OP, 'J': 2 << 10}),  # C-17: J -> Rs 0010x->0011x
    (r'\*\*', {'I': 8 << OP, 'C': 4 << OP, 'CR': 6 << OP, 'J': 2 << 20}), # C-16: J -> type+2
    (r'MR[0-9:]+ [-+]= |, MR', {'U': 16 << OP, 'I': 8 << OP, 'C': 4 << OP, 'CR': 6 << OP}),
    (r'FR.* = .*\*', {'T': 2 << OP}),                                      # C-16
    (r'= R[0-9:]+ \* R', {'U': 16 << OP, 'NU': 1 << OP, 'I': 8 << OP, 'S': 4 << OP, 'T': 2 << OP}),
    (r'FR.*=', {'T': 2 << OP}),                                            # C-12 note 1
    (r'= LOGB', {'S': 2 << OP}),                                           # C-11
    (r'= FIX', {'T': 2 << OP}),
    (r'= EXPAND', {'I': 1 << OP, 'IU': 3 << OP}),                          # C-9 note 1
    (r'R[0-9:]+ = COMPACT [LQ]R', {'U': 1, 'IS': 2, 'ISU': 3}),            # C-8 Rn field
    (r'= COMPACT', {'I': 1 << OP, 'T': 4 << OP, 'IS': 5 << OP, 'ISU': 7 << OP}),  # C-10 note 3
    (r'PR[01] \+=', {'U': 2 << OP}),                                       # C-9 note 5
    (r'= SUM', {'U': 2 << OP}),
    (r'COMP ?\(R', {'U': 2 << OP}),                                        # C-6
    (r'= ABS \(.*\+', {'X': 1 << OP}),                                     # C-7 notes 4
    (r'= ABS \(.*-', {'X': 1 << OP, 'U': 2 << OP}),
    (r'\)/2', {'T': 1 << OP, 'U': 2 << OP}),                               # C-7 note 5
    (r'= (MAX|MIN) \(R', {'Z': 1 << OP, 'U': 2 << OP}),                    # C-7 note 6
    (r'(\+|-|INC|DEC)', {'S': 1 << OP, 'SU': 3 << OP}),                    # C-7 note 3
    (r'= FEXT|= GETBITS', {'SE': 1 << OP}),
    (r'THR.* = R', {'I': 1 << 10}),                                        # C-25 op 0001I
    (r'= ACS', {'TMAX': 1 << 10}),                                         # C-13
]
IALU = {'CJMP': 2 << 21, 'CB': 4 << 21, 'BR': 6 << 21, 'U': 1 << 21}      # C-28
WHOLE = ('SU', 'IS', 'ISU', 'IU', 'CR', 'NU', 'SE', 'ZE', 'ZF', 'NF', 'TMAX', 'CJMP', 'CB', 'BR')


def expected_mask(base, opts):
    flat = []
    for o in opts:
        flat += [o] if o in WHOLE else re.findall(r'CR|NU|NF|SE|ZE|ZF|[UICJSTXZ]', o)
    mask = 0
    if 'NF' in flat:
        mask |= 1 << 30
        flat.remove('NF')
    if not flat:
        return mask
    if re.match(r'[JK]\d+ = |COMP\([JK]', base):
        return mask + sum(IALU[o] for o in flat)
    body = re.sub(r'^(XY|X|Y) ?', '', base)
    for pattern, table in RULES:
        if re.search(pattern, body):
            combo = ''.join(flat)
            if combo in table:
                return mask | table[combo]
            parts = []
            for o in flat:
                parts += [o] if o in table else list(o)
            for o in parts:
                mask |= table[o]
            return mask
    raise KeyError(base)


class OptionBitTests(unittest.TestCase):
    def test_option_masks(self):
        lines = [l for l in Path(__file__).with_name('appendix_a_options.txt').read_text().splitlines()
                 if l and not l.startswith('#')]
        checked, failures = 0, []
        for line in lines:
            base, opts = line, []
            while (m := re.search(r'\s*\(([A-Z]+)\)$', base)) and not re.search(r'R\d', m[1]):
                opts.insert(0, m[1])
                base = base[:m.start()]
            if not opts or 'CUT' in line:
                continue
            want = expected_mask(base, opts)
            with_opts = packet(line, {'start': 0x100}, 0x100)[0][0]
            without = packet(base.strip(), {'start': 0x100}, 0x100)[0][0]
            checked += 1
            if with_opts ^ without != want:
                failures.append(f'{line}: {with_opts ^ without:#x} != {want:#x}')
        self.assertGreater(checked, 5500)
        self.assertEqual(failures, [])
