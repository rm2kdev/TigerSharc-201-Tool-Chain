import copy
import json
from pathlib import Path
import tempfile
import unittest
from ts201ld import make_object,link,write_object,read_object,LinkError


MAIN='.global entry\n.extern value\n.section text, code, 4\nentry: J0 = value;;\nJ1 = [J0 + 0];;\nIDLE;;'
DATA='.global value\n.section data, data, 16\nvalue: .word 42'


class LinkerTests(unittest.TestCase):
    def pair(self): return [make_object(MAIN,'main.asm'),make_object(DATA,'data.asm')]

    def test_relocations_and_placement(self):
        image,source,report=link(self.pair(),entry='entry',startup=False,code_base=0x1004,data_base=0x40103)
        self.assertEqual(image.symbols['entry'],0x1004)
        self.assertEqual(image.symbols['value'],0x40110)
        self.assertEqual(report['placements'][1]['alignment'],16)

    def test_roundtrip(self):
        obj=self.pair()[0]
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.tso'; write_object(obj,p); self.assertEqual(read_object(p),obj)

    def test_duplicate_exports(self):
        with self.assertRaisesRegex(LinkError,'duplicate export'): link(self.pair()+[make_object(DATA)])

    def test_undefined(self):
        with self.assertRaisesRegex(LinkError,'unresolved import'): link(self.pair()[:1],entry='entry')

    def test_bank_crossing(self):
        with self.assertRaisesRegex(LinkError,'crosses bank'): link(self.pair(),entry='entry',startup=False,code_base=0x1ffff)

    def test_overlap(self):
        with self.assertRaisesRegex(LinkError,'overlapping'): link(self.pair(),entry='entry',startup=False,code_base=0x40000,data_base=0x40000)

    def test_branch_data(self):
        obj=make_object('.global entry\n.extern value\n.section text, code\nentry: JUMP value (NP);;')
        with self.assertRaisesRegex(LinkError,'packet start'): link([obj,self.pair()[1]],entry='entry',startup=False)

    def test_branch_extension(self):
        obj=make_object('.global entry\n.section text, code\nentry: JUMP entry + 1 (NP);;')
        with self.assertRaisesRegex(LinkError,'packet start'): link([obj],entry='entry',startup=False)

    def test_corrupt_relocations(self):
        objects=self.pair(); objects[0]['relocations']=[]
        with self.assertRaisesRegex(LinkError,'inconsistent object relocations'): link(objects,entry='entry')

    def test_private_symbols(self):
        a=make_object('.global entry\n.section t, code\nentry: JUMP local (NP);;\nlocal: IDLE;;')
        b=make_object('.section t, code\nlocal: IDLE;;')
        image,_,_=link([a,b],entry='entry',startup=False)
        self.assertNotEqual(image.symbols['_m0_local'],image.symbols['_m1_local'])

    def test_literal_layout(self):
        with self.assertRaisesRegex(LinkError,'undefined symbol'): make_object('.section text, code, symbol\ns: IDLE;;')

    def test_abi(self):
        objects=self.pair(); objects[0]['abi']='vendor'
        with self.assertRaisesRegex(LinkError,'incompatible object ABI'): link(objects)

    def test_empty_export(self):
        with self.assertRaisesRegex(LinkError,'undefined export'): make_object('.global absent\n.section t, code\ns: IDLE;;')

    def test_stack_alignment(self):
        with self.assertRaisesRegex(LinkError,'multiple of four'): link(self.pair(),entry='entry',stack_words=17)

    def test_no_input(self):
        with self.assertRaisesRegex(LinkError,'no input'): link([])

    def test_undeclared_reference(self):
        with self.assertRaisesRegex(LinkError,'undefined symbol'):
            make_object('.section t, code\ns: J0 = hidden;;\nIDLE;;')

    def test_hex_literal_not_identifier(self):
        a=make_object('.global entry\n.section t, code\nentry: J0 = 0xabc;;\nIDLE;;\n.section d, data\nxabc: .word 0xabc')
        image,source,_=link([a],entry='entry',startup=False)
        self.assertEqual(image.sections[-1].words,[0xabc])
        self.assertIn('0xabc',source)

    def test_malformed_type(self):
        a=self.pair(); a[0]['types']={'entry':['nonsense']}
        with self.assertRaisesRegex(LinkError,'unknown type'): link(a,entry='entry')

    def test_typed_code_data_mismatch(self):
        a=self.pair(); a[0]['types']={'value':['function',['int',False],[]]}
        with self.assertRaisesRegex(LinkError,'symbol kind mismatch'): link(a,entry='entry')

    def test_register_range_is_not_a_label(self):
        a=make_object('.global entry\n.section t, code\nentry: J0 = data;;\nXYR3:0 = Q[J0 + 0];;\nXR7:4 = [J0 + 0];;\nIDLE;;\n.section d, data, 4\ndata: .zero 4')
        self.assertNotIn('XYR3',a['symbols'])
        self.assertNotIn('XR7',a['symbols'])
        image,_,_=link([a],entry='entry',startup=False)
        self.assertEqual(len(image.packets),4)


def image_words(image,name):
    address=image.symbols[name]
    return next(p for p in image.packets if p['word_address']==address)['word_count']


class RelaxationTests(unittest.TestCase):
    CALLER='.global entry\n.extern far\n.section t, code\nentry: CALL far (NP);;\nIDLE;;'

    def callee(self,alignment=4):
        return make_object(f'.global far\n.section t, code, {alignment}\nfar: CJMP (NP)(ABS);;','callee.asm')

    def test_near_call_uses_one_slot_form(self):
        image,source,report=link([make_object(self.CALLER),self.callee()],entry='entry',startup=False)
        self.assertEqual(image_words(image,'entry'),1)
        self.assertIn('CALL _m1_far (NP) (S);;',source)
        self.assertEqual(report['placements'][0]['words'],2)
        self.assertEqual(image.symbols['far'],0x104)
        unrelaxed,source,_=link([make_object(self.CALLER),self.callee()],entry='entry',startup=False,relax_branches=False)
        self.assertEqual(image_words(unrelaxed,'entry'),2)
        self.assertNotIn('(S)',source)

    def test_range_limit(self):
        # 0x4000 words away does not fit the signed 15-bit displacement; 0x3fff does.
        far,_,_=link([make_object(self.CALLER),self.callee(0x4000)],entry='entry',startup=False,code_base=0)
        self.assertEqual(far.symbols['far']-far.symbols['entry'],0x4000)
        self.assertEqual(image_words(far,'entry'),2)
        caller=make_object('.global entry\n.extern far\n.section t, code\nentry: J0 = 1;;\ncall: CALL far (NP);;\nIDLE;;')
        near,_,_=link([caller,self.callee(0x4000)],entry='entry',startup=False,code_base=0)
        self.assertEqual(near.symbols['far']-near.symbols['_m0_call'],0x3fff)
        self.assertEqual(image_words(near,'_m0_call'),1)

    def test_startup_call_and_backward_branch(self):
        loop=make_object('.global _c_main\n.section t, code\n_c_main: J0 = 1;;\nhere: J0 = J0 + 1;;\nJUMP here (NP);;')
        image,source,_=link([loop],fold_branches=False)
        self.assertIn('CALL _m0__c_main (NP) (S);;',source)
        self.assertIn('JUMP _m0_here (NP) (S);;',source)

    def test_predicted_branches_never_share_a_quad(self):
        # Shrinking both calls would end them in one aligned quad; keep them apart.
        a=make_object('.global entry\n.extern far\n.section t, code\nentry: CALL far;;\nNOP;;\nNOP;;\nCALL far;;\nIDLE;;')
        image,_,_=link([a,self.callee()],entry='entry',startup=False)
        ends=[(p['word_address']+p['word_count']-1)//4 for p in image.packets if p['text'].startswith('CALL')]
        self.assertEqual(len(ends),len(set(ends)))


class FoldTests(unittest.TestCase):
    def fold(self,body,name='unit.c'):
        obj=make_object('.global entry\n.section t, code\n'+body,name)
        return link([obj],entry='entry',startup=False,relax_branches=False)

    def test_jump_absorbs_target_packet(self):
        image,source,_=self.fold('entry: JUMP next (NP)(NF)(S);;\nIDLE;;\nnext: J1 = J1 + 1;;\nJ2 = 6;;\nIDLE;;')
        self.assertIn('JUMP _m0_next + 1 (NP)(S); J1 = J1 + 1;;',source)
        self.assertEqual(image_words(image,'entry'),2)
        unfolded,_,_=link([make_object('.global entry\n.section t, code\nentry: JUMP next (NP)(NF)(S);;\nnext: J1 = 1;;\nIDLE;;','u.c')],
                          entry='entry',startup=False,fold_branches=False)
        self.assertEqual(image_words(unfolded,'entry'),1)

    def test_line_nf_follows_the_absorbed_operations(self):
        _,source,_=self.fold('entry: JUMP next (NP)(S);;\nnext: J1 = J1 + 1 (NF); K1 = K1 + 1 (NF);;\nIDLE;;')
        self.assertIn('JUMP _m0_next + 2 (NP)(S)(NF); J1 = J1 + 1 (NF); K1 = K1 + 1 (NF);;',source)
        _,source,_=self.fold('entry: JUMP next (NP)(S);;\nnext: J1 = J1 + 1 (NF); K1 = K1 + 1;;\nIDLE;;')
        self.assertIn('JUMP _m0_next (NP)(S);;',source)

    def test_unfoldable_targets(self):
        for target in ('LC0 = J1;;','IF JEQ, JUMP entry (NP);;','CJMP = J1;;','JSTAT = J1;;',
                       'J1 = [J2 + 1]; K1 = [K2 + 1]; XR0 = R1 + R2; YR0 = R1 + R2;;'):
            with self.subTest(target=target):
                _,source,_=self.fold(f'entry: JUMP next (NP)(S);;\nnext: {target}\nIDLE;;')
                self.assertIn('JUMP _m0_next (NP)(S);;',source)

    def test_jump_lines_with_operations(self):
        cases={
            # Independent registers and flags: folded.
            'JUMP next (NP)(S); J5 = J6 + 1;;\nnext: K1 = K2 + 1;;': True,
            # Disjoint literal absolute words (J31 and K31 read as zero).
            'JUMP next (NP)(S); [J31 + 5] = XR0;;\nnext: YR1 = [K31 + 6];;': True,
            'JUMP next (NP)(S); Q [J31 + 4] = XR3:0;;\nnext: YR1 = [K31 + 8];;': True,
            'JUMP next (NP)(S); Q [J31 + 4] = XR3:0;;\nnext: YR1 = [K31 + 7];;': False,
            # Different base registers may alias.
            'JUMP next (NP)(S); Q [K27 + 4] = XR3:0;;\nnext: J6 = [J27 + 8];;': False,
            # Reads a register the jump line writes.
            'JUMP next (NP)(S); J1 = J2 + 1;;\nnext: J3 = J1 + 1;;': False,
            'JUMP next (NP)(S); J1 = [J2 += 1];;\nnext: K3 = J2;;': False,
            # Both update the J flags.
            'JUMP next (NP)(S); J1 = J1 + 1;;\nnext: COMP(J2, J3);;': False,
            # Overlapping or unknown addresses.
            'JUMP next (NP)(S); Q [J27 + 4] = XR3:0;;\nnext: J6 = [J27 + 4];;': False,
            'JUMP next (NP)(S); [J3 + 0] = XR4;;\nnext: J9 = [J12 + 0];;': False,
            'JUMP next (NP)(S); J1 = [J3 + 0];;\nnext: K9 = [K12 + 0];;': False,
            # A compute block the jump line loads into.
            'JUMP next (NP)(S); XR0 = [J1 + 0];;\nnext: XR2 = R1 + R3;;': False,
            # A call runs the callee's first packet in its own line ...
            'CALL next (NP)(S); J5 = J6 + 1;;\nnext: K1 = K2 + 1;;': True,
            # ... unless that packet reads the return address the call writes.
            'CALL next (NP)(S);;\nnext: J1 = CJMP;;': False,
        }
        for body,folded in cases.items():
            with self.subTest(body=body):
                _,source,_=self.fold('entry: '+body+'\nIDLE;;')
                self.assertEqual('_m0_next + 1' in source,folded)

    def test_assembly_objects_and_section_ends_are_kept(self):
        _,source,_=self.fold('entry: JUMP next (NP)(S);;\nnext: J1 = 1;;\nIDLE;;','unit.asm')
        self.assertIn('JUMP _m0_next (NP)(S);;',source)
        _,source,_=self.fold('entry: JUMP next (NP)(S);;\nIDLE;;\nnext: J1 = 1;;')
        self.assertIn('JUMP _m0_next (NP)(S);;',source)
        _,source,_=self.fold('entry: IF JEQ, JUMP next (NP)(S);;\nnext: J1 = 1;;\nIDLE;;')
        self.assertNotIn('next +',source)


class GarbageCollectionTests(unittest.TestCase):
    UNIT=('.global entry, used, unused, table\n.section text, code\n'
          'entry: J0 = table;;\nCALL used (NP);;\nIDLE;;\n'
          'used: J1 = 1;;\nCJMP (NP)(ABS);;\n'
          'unused: J2 = 2;;\nJUMP tail (NP);;\n'
          'dead_block: J3 = 3;;\ntail: CJMP (NP)(ABS);;\n'
          '.section data, data\nlost: .word 1, 2, 3\ntable: .word used, 5\nfree: .zero 7')

    def test_unreached_functions_and_data_are_discarded(self):
        image,source,report=link([make_object(self.UNIT,'unit.c')],entry='entry',startup=False,keep=(),fold_branches=False)
        for name in ('unused','dead_block','tail','lost','free'):
            self.assertNotIn(f'_m0_{name}',image.symbols)
        self.assertNotIn('unused',report['exports'])
        self.assertEqual(report['discarded']['symbols'],['_m0_dead_block','_m0_free','_m0_lost','_m0_tail','_m0_unused'])
        self.assertEqual(report['discarded']['data_words'],10)
        # table keeps its offset modulo four: three discarded words become padding.
        self.assertEqual(image.symbols['table']%4,3)
        self.assertEqual(image.symbols['used'],image.symbols['entry']+4)

    def test_keep_and_fall_through(self):
        image,_,report=link([make_object(self.UNIT,'unit.c')],entry='entry',startup=False,keep=['unused'])
        for name in ('unused','dead_block','tail'):
            self.assertIn(f'_m0_{name}',image.symbols)
        with self.assertRaisesRegex(LinkError,'not exported'):
            link([make_object(self.UNIT,'unit.c')],entry='entry',startup=False,keep=['missing'])

    def test_assembly_sections_stay_whole_and_default_keeps_everything(self):
        image,_,report=link([make_object(self.UNIT,'unit.asm')],entry='entry',startup=False,keep=())
        self.assertIn('_m0_unused',image.symbols)
        self.assertEqual(report['discarded']['code_words'],0)
        image,_,report=link([make_object(self.UNIT,'unit.c')],entry='entry',startup=False)
        self.assertIn('_m0_unused',image.symbols)
        self.assertNotIn('discarded',report)


if __name__=='__main__': unittest.main()
