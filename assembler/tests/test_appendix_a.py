"""Repeatable syntax coverage; deliberately separate from encoding conformance.

Cases are original concrete statements corresponding to Appendix A listings
A-1..A-18 of PR Rev 1.1. Mandatory choices expanded; optional features defaulted.
262 entries count J and K separately and include the DO/ELSE packet entries.
The one contradictory entry is preserved as a rejection, not silently rewritten.
"""
import json
from pathlib import Path
import unittest
from ts201.assembler import assemble, AssemblyError
from test_assembler import program

REPORT = {}


class AppendixTests(unittest.TestCase):
    def test_appendix_representatives(self):
        cases=json.loads(Path(__file__).with_name('appendix_a_cases.json').read_text())
        entries={}; accepted=0
        for case in cases:
            entry=case['entry']; entries.setdefault(entry,False)
            with self.subTest(entry=entry,source=case['source']):
                if case['expected_accept']:
                    image=assemble(program(case['source']+';;'))
                    self.assertTrue(image.packets)
                    entries[entry]=True; accepted+=1
                else:
                    self.assertEqual(entry,18749)
                    with self.assertRaisesRegex(AssemblyError,'same accumulator'):
                        assemble(program(case['source']+';;'))
        # The A-7 / 10-133 selected-MRb spelling conflicts with C-16 and the
        # actual full-quad example on 10-137. This corrected form is supported.
        assemble(program('X R9:8 = MR3:0, MR3:0 += R1:0 * R5:4;;'))
        REPORT.update(syntax_entries=len(entries),entries_with_accepted_representative=sum(entries.values()),
                      representative_variants=len(cases),accepted_variants=accepted,
                      documented_syntax_conflicts=1,
                      scope='Acceptance only. Optional types/options are not exhaustive; not hardware conformance.',
                      conflict='A-7/10-133 MRb transfer with MR3:0 accumulation; supported canonical MR3:0 form on C-16/10-137.')
        self.assertEqual((len(entries),sum(entries.values()),len(cases),accepted),(262,261,638,637))

    def test_compute_block_variants(self):
        cases=json.loads(Path(__file__).with_name('appendix_a_cases.json').read_text())
        for case in cases:
            if not case['expected_accept'] or not case['source'].startswith('X '): continue
            for block in ('Y','XY'):
                if block=='XY' and 'STAT' in case['source']:
                    continue  # A-10: {X|Y}STAT only
                with self.subTest(entry=case['entry'],block=block):
                    assemble(program(block+case['source'][1:]+';;'))


class AppendixOptionCorpusTests(unittest.TestCase):
    """Every Appendix A listing expanded with all optional prefixes/options
    (tests/appendix_a_options.txt). Deliberate exclusions, not in the file:
    (I)(CR) (5-17: CR only for fractional), 'Rsd = MRb, MR3:0 += ...'
    (C-16/10-137 conflict), unprefixed/XY STAT (A-10 '{X|Y}'), XY stores."""

    def test_option_corpus(self):
        from ts201.assembler import packet
        lines=[l for l in Path(__file__).with_name('appendix_a_options.txt').read_text().splitlines()
               if l and not l.startswith('#')]
        self.assertGreater(len(lines),8000)
        failures=[]
        for line in lines:
            try:
                packet(line,{'start':0x100},0x100)
            except AssemblyError as error:
                failures.append(f'{line}: {error}')
        self.assertEqual(failures,[])
