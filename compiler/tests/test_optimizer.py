import re
import unittest
from ts201cc import compile_source,CompileError

class OptimizerTests(unittest.TestCase):
    def test_volatile_typedef_stays_in_memory(self):
        # x++ reads and writes x; x+x must keep two separately observable reads.
        for level in (1,2):
            _,asm=compile_source('typedef volatile int V; int main(void){V x=1; x++; return x+x;}',optimize=level)
            frame=r'J2[67] \+ \d+'
            self.assertGreaterEqual(len(re.findall(r'XR\d+ = \['+frame+r'\]',asm)),3,asm)
            self.assertGreaterEqual(len(re.findall(r'\['+frame+r'\] = XR\d+',asm)),2,asm)

    def test_intrinsic_diagnostics(self):
        for source in ('int main(void){return __builtin_ts201_mul_q16(1);}',
                       'int main(void){return __builtin_ts201_mul_q16((int*)0,1);}'):
            with self.assertRaisesRegex(CompileError,'two integer arguments'):compile_source(source)

    def test_constant_division_has_no_loop(self):
        for divisor in (2,7,10,65536):
            _,asm=compile_source(f'unsigned f(unsigned a){{return a/{divisor}U;}}')
            self.assertNotIn('div_valid',asm)
        _,baseline=compile_source('unsigned f(unsigned a){return a/7U;}',optimize=0)
        self.assertIn('div_valid',baseline)

    def test_small_expression_inlining_is_opt_in(self):
        source='static int f(int a){return a*3+1;}int main(void){return f(4);}'
        _,first=compile_source(source,optimize=1);_,second=compile_source(source,optimize=2)
        self.assertIn('CALL _s_f',first);self.assertNotIn('CALL _s_f',second)

    def test_address_exposed_local_stays_addressable(self):
        _,asm=compile_source('int f(int*p){return ++*p;}int main(void){int x=1;f(&x);return x;}')
        main=asm[asm.index('_c_main:'):]
        # x lives in the frame: its address is formed and it is reloaded after the call.
        self.assertRegex(main,r'J\d+ = J27 \+ \d+')
        self.assertRegex(main[main.index('CALL _c_f'):],r'XR\d+ = \[J27 \+ \d+\]')

    def test_counted_loop_uses_hardware_counter(self):
        _,asm=compile_source('int a[64]; void f(int n){int i; for(i=0;i<n;i++) a[i]=i;}',optimize=1)
        self.assertIn('LC0 = ',asm)
        self.assertRegex(asm,r'IF N?LC0E, JUMP')
        self.assertNotRegex(asm,r'COMP\(J\d+, J\d+\)')

    def test_memcpy_uses_dab_for_misaligned_sources(self):
        src='void *memcpy(void*,const void*,unsigned); void f(int*d,int*s,unsigned n){memcpy(d,s,n);}'
        _,asm=compile_source(src,optimize=2)
        self.assertRegex(asm,r'XR\d+:\d+ = DAB Q\[J0 \+= 4\]')
        from ts201cc.compiler import Compiler
        _,asm=Compiler('t.c',optimize=2,no_dab=True).compile(src)
        self.assertNotIn('DAB',asm)

    def test_field_extract_uses_fext(self):
        _,asm=compile_source('int f(int x){return (x>>16)&63;} unsigned g(unsigned x){return (x>>3)&0x1ff;}',optimize=1)
        self.assertEqual(asm.count('FEXT'),2)
        self.assertIn('= 4102',asm)    # control word: pos 16 << 8 | len 6

    def test_intra_function_branches_are_short(self):
        # One-slot 15-bit form: the loop branch shares a packet with loop work.
        _,asm=compile_source('int a[64]; void f(int n){int i; for(i=0;i<n;i++) a[i]=i;}',optimize=1)
        self.assertRegex(asm,r'IF N?LC0E, JUMP \S+ \(NP\)\(NF\)\(S\);')
        self.assertNotRegex(asm,r'JUMP _cc_\S+ \(NP\)\(NF\);')

    def test_loop_with_call_keeps_explicit_test(self):
        # A callee may use the loop counters, so loops containing calls do not.
        _,asm=compile_source('void g(int); void f(int n){int i; for(i=0;i<n;i++) g(i);}',optimize=2)
        self.assertNotIn('LC0',asm)

    def test_memcpy_and_memset_expand_inline(self):
        src=('extern void *memcpy(void*,const void*,unsigned); extern void *memset(void*,int,unsigned);'
             'int a[40],b[40]; void f(unsigned n){memcpy(a,b,n); memset(b,0,n);}')
        obj,asm=compile_source(src,optimize=2)
        self.assertNotIn('CALL',asm)
        self.assertIn('Q [',asm)          # quad-word fast path
        self.assertNotIn('_c_memcpy',obj['imports'])
        # A unit that defines memcpy itself keeps calling it (no builtin, no -O1 inlining).
        _,own=compile_source('void *memcpy(void*d,const void*s,unsigned n){return d;} int a[4],b[4]; void f(void){memcpy(a,b,4);}',optimize=1)
        self.assertIn('CALL _c_memcpy',own)

    def test_clamps_become_min_max(self):
        _,asm=compile_source('int f(int y,int c){if(y<c+1)y=c+1; return y;} int g(int y,int c){if(y>=c)y=c-1; return y;}',optimize=2)
        self.assertIn('MAX(',asm); self.assertIn('MIN(',asm)
        self.assertNotRegex(asm,r'IF N?[JKX]A?(LT|LE|EQ), JUMP')

    def test_nested_clamps_and_abs_are_branch_free(self):
        src=('static int clip(int s){if(s>65535)return 65535;if(s<-65536)return -65536;return s;}'
             'int f(int x){return clip(x)+1;} int g(int x){if(x>9)x=9;else if(x<-9)x=-9;return x;}'
             'int h(int v){if(v<0)return -v;return v;} int k(int*p,int i){if(i<0)i=-i;return p[i];}')
        _,asm=compile_source(src,optimize=2)
        self.assertNotRegex(asm,r'IF N?[JKX]A?(LT|LE|EQ), JUMP')
        # g's symmetric clamp is one CLIP; f's asymmetric one stays MIN + MAX.
        self.assertEqual(asm.count('MAX('),2); self.assertEqual(asm.count('MIN('),1)
        self.assertEqual(len(re.findall(r'= CLIP R\d+ BY R\d+',asm)),1,asm)
        self.assertIn('= ABS R',asm)

    def test_q16_multiply_by_constant_is_one_fractional_multiply(self):
        _,asm=compile_source('int f(int x){return __builtin_ts201_mul_q16(x,1800)+__builtin_ts201_mul_q16(-65535,x);}',optimize=1)
        self.assertEqual(len(re.findall(r'R\d+ = R\d+ \* R\d+ \(T\)',asm)),2,asm)
        self.assertNotIn('ASHIFT',asm)
        self.assertIn('= 58982400',asm)    # 1800 << 15
        # -65536 << 15 would need the fractional -1 * -1 case: keep the long shift.
        _,asm=compile_source('int f(int x){return __builtin_ts201_mul_q16(x,-65536);}',optimize=1)
        self.assertIn('ASHIFT R1:0 BY -16',asm)

    def test_unrolled_copies_share_one_address_phi(self):
        # Identical phis (one per unrolled copy) merge, so the copies address
        # one base with distinct offsets and their loads need not wait for stores.
        _,asm=compile_source('void g(int*p,int n){int i;for(i=0;i<n;i++)p[i]=p[i]*3+1;}',optimize=2)
        bases={m for m in re.findall(r'XR\d+ = \[([JK]\d+) \+ [123]\]',asm)}-{'J27'}
        self.assertEqual(len(bases),1,asm)

    def test_local_read_before_any_assignment_compiles(self):
        # Undefined behaviour, but it must not crash register allocation.
        for level in (0,1,2):
            _,asm=compile_source('int g(int a){int x;return x;} int f(int a){int x;if(a)x=3;return x+a;}',optimize=level)
            self.assertIn('_c_g:',asm)

    def test_restrict_pointers_do_not_alias(self):
        body='{S*E=(S*)st;E->a=E->a+p[0]*x;E->b=E->b+p[1]*E->a;E->c=p[2]+E->b;return E->c+p[3];}'
        head='typedef struct S{int a,b,c;}S; int f(int x,int*%s p,int*%s st)'
        _,fast=compile_source(head%('restrict','restrict')+body.replace('S*E','S*restrict E'),optimize=2)
        _,slow=compile_source(head%('','')+body,optimize=2)
        self.assertLessEqual(fast.count(';;')+5,slow.count(';;'),fast)
        # A restrict pointer that is reassigned gives no guarantee.
        _,re_=compile_source('int f(int*restrict p,int*restrict q){p=p+1;*q=1;return *p;}',optimize=2)
        _,plain=compile_source('int f(int*p,int*q){p=p+1;*q=1;return *p;}',optimize=2)
        self.assertEqual(re_,plain)

    def test_pointer_to_volatile_stays_in_a_register(self):
        _,asm=compile_source('void f(volatile int*r,int n){volatile int*p=r;int i;for(i=0;i<n;i++){*p=i;p=p+1;}}',optimize=2)
        self.assertNotRegex(asm,r'\[J27 \+ \d+\] = [JK]\d+')   # the pointer is not spilled

    def test_plus_minus_one_uses_inc_dec(self):
        _,asm=compile_source('int f(int x,int y){return (x*y+1)*(x*y-1);}',optimize=2)
        self.assertIn('= INC R',asm); self.assertIn('= DEC R',asm)

    def test_memset_stores_quads(self):
        _,asm=compile_source('void *memset(void*,int,unsigned); void f(int*d,int v,unsigned n){memset(d,v,n);}',optimize=2)
        self.assertRegex(asm,r'IF NLC0E, JUMP \S+ \(NP\)\(NF\)\(S\); Q \[[JK]\d+ \+= 4\] = XR\d+:\d+')

    def test_noreturn_call_keeps_function_leaf(self):
        _,asm=compile_source('extern void exit(int); int f(int x){if(x<0) exit(3); return x*2;}',optimize=2)
        self.assertNotIn('= CJMP',asm)   # the return address is never saved
        self.assertIn('CALL _c_exit',asm)

    def test_tail_call_reuses_incoming_argument_slots(self):
        _,asm=compile_source('extern int g(int,int,int); int f(int x,int y,int z){return g(y,x,z+1);}',optimize=1)
        f=asm[asm.index('_c_f:'):]
        self.assertIn('JUMP _c_g',f); self.assertNotIn('CALL',f); self.assertNotIn('CJMP',f)
        # A local whose address reaches the callee keeps its frame: no tail call.
        _,asm=compile_source('extern int g(int*); int f(int x){int t=x; return g(&t);}',optimize=1)
        self.assertIn('CALL _c_g',asm); self.assertNotIn('JUMP _c_g',asm)
        # The callee needs more argument words than the caller received.
        _,asm=compile_source('extern int g(int,int,int,int,int); int f(int x){return g(x,x,x,x,x);}',optimize=1)
        self.assertIn('CALL _c_g',asm)

    def test_return_address_restores_with_one_load(self):
        _,asm=compile_source('extern int g(int); int f(int x){return g(x)+1;}',optimize=1)
        self.assertRegex(asm,r'CJMP = \[J27 \+ \d+\]'); self.assertNotIn('(CJMP)',asm)

    def test_branch_packet_takes_fall_through_work_as_else(self):
        src='int a[10]; int s; int f(int x,int y){int r; if(x<y){r=a[x]+3; s=r;} else r=y*7; return r+x;}'
        _,asm=compile_source(src,optimize=2)
        self.assertRegex(asm,r'IF N?XA?L[TE], JUMP [^;]*; ELSE ')

    def test_early_exit_skips_saves(self):
        src=('extern int active; extern int work(int);\n'
             'int f(int x,int y){int a; if(x>y) return y; a=work(x); a+=work(y+a); return a*x;}\n'
             'void g(void){int a; if(!active) return; a=work(1); active=work(a)+a;}')
        _,asm=compile_source(src,optimize=2)
        for name,nxt in (('_c_f:','_c_g:'),('_c_g:',None)):
            body=asm[asm.index(name):asm.index(nxt) if nxt else None]
            head=body[:body.index('CJMP (NP)(ABS)')]
            # The early return comes before any frame, CJMP save or callee-saved store.
            self.assertNotIn('J27 = J27 -',head); self.assertNotIn('= CJMP',head)
            self.assertRegex(body.split(':',1)[1],r'^[^:]*IF N?[JKX]A?(LT|LE|EQ), CJMP \(NP\)\(ABS\)')

    def test_saves_use_both_ialus(self):
        # Four save-area transfers: half go through a K copy of J27 and pair with J ones.
        src='extern int work(int); int st; int f(int *p,int k){st+=work(p[1]+k); return p[2]+st+k;}'
        _,asm=compile_source(src,optimize=2)
        self.assertRegex(asm,r'K\d+ = J27;')
        self.assertRegex(asm,r'\[K\d+ \+ \d+\] = [^;]*; \[J27 \+ \d+\] = |\[J27 \+ \d+\] = [^;]*; \[K\d+ \+ \d+\] = ')

    def test_noinline_attribute_and_cold_paths(self):
        src=('static __attribute__((noinline)) int sq(int x){return x*x+1;}\n'
             'static int tw(int x){return x*2;}\n'
             'int f(int a){return sq(a)+tw(a);}')
        _,asm=compile_source(src,optimize=2)
        self.assertIn('CALL _s_sq',asm); self.assertNotIn('CALL _s_tw',asm)
        # A path that cannot return (it ends in a noreturn call) keeps its callees out of line.
        src=('extern _Noreturn void halt(int); int log_[8];\n'
             'static void note(int v){log_[0]=v; log_[1]+=v; log_[2]^=v; log_[3]=log_[2]+1;}\n'
             'int f(int a){if(a<0){note(a); halt(a);} note(a+1); return a;}')
        _,asm=compile_source(src,optimize=2)
        self.assertEqual(asm.count('CALL _s_note'),1)

    def test_single_use_global_address_is_absolute(self):
        _,asm=compile_source('extern int active; int f(void){return active+1;}',optimize=2)
        self.assertRegex(asm,r'= \[[JK]31 \+ _c_active\]'); self.assertNotRegex(asm,r'[JK]\d+ = _c_active')

    def test_inlining_is_not_capped_by_call_count(self):
        # Five small callees in straight-line code, then one inside a loop: all inline.
        src=('static int c(int x){if(x>9)return 9; if(x<0)return 0; return x;}\n'
             'int buf[64];\n'
             'int f(int a,int b,int n){int i,s=c(a)+c(b)+c(a+b)+c(a-b)+c(a*b);\n'
             'for(i=0;i<n;i++) buf[i]=c(buf[i]+s); return s;}')
        _,asm=compile_source(src,optimize=2)
        self.assertNotIn('CALL _s_c',asm)

    def test_unaliased_static_global_is_hoisted(self):
        src='static int step; int out[64]; void f(int *p,int n){int i; for(i=0;i<n;i++) p[i]=step*i;}'
        _,asm=compile_source(src,optimize=1)
        code=asm[asm.index('_c_f:'):asm.index('.section data')]
        # `step` is loaded once, before the loop, although the loop stores through p.
        self.assertEqual(len(re.findall(r'_s_step\b',code)),1)   # `= _s_step` or `[J31 + _s_step]`

    def test_ring_buffer_taps_use_both_ialus(self):
        # Every tap hangs off one state pointer and one write index: the taps
        # split between the IALUs (hub copies) instead of all running on one.
        src=('typedef struct{int pre[8192];int da[8192];int db[8192];int w;int lp;}P;'
             'int f(int x,int*p,int*st){P*s=(P*)st;int w=s->w,a,b,c,d;a=s->da[(w-7182)&8191];b=s->db[(w-6803)&8191];'
             'c=s->da[(w-429)&8191];d=s->db[(w-1996)&8191];s->pre[w&8191]=x;s->da[w&8191]=a+p[0];s->db[w&8191]=b+p[1];'
             's->w=(w+1)&8191;s->lp=c-d;return a+b+c+d;}')
        _,asm=compile_source(src,optimize=2)
        self.assertRegex(asm,r'XR\d+ = \[J\d+ \+ J\d+\]'); self.assertRegex(asm,r'XR\d+ = \[K\d+ \+ K\d+\]')
        # Taps are indexed accesses from a shared `base + 8192` anchor: no
        # per-tap address add and no extension word.
        self.assertNotRegex(asm,r'XR\d+ = \[[JK]\d+ \+ (8192|16384)\]')
        self.assertRegex(asm,r'[JK]\d+ = [JK]\d+ \+ 8192 \(NF\)')

    def test_bounds_check_pair_is_one_unsigned_compare(self):
        _,asm=compile_source('int f(int i,int x){if(i<0||i>=16)return x;return x+i;}',optimize=2)
        self.assertEqual(len(re.findall(r'COMP\(',asm)),1,asm); self.assertIn('(U)',asm)
        _,asm=compile_source('int t[16]; int f(int i){if(i>=0&&i<16)return t[i];return -1;}',optimize=2)
        self.assertEqual(len(re.findall(r'COMP\(',asm)),1,asm)

    def test_jump_table_loads_cjmp(self):
        src='int f(int k,int x){switch(k){case 0:return x+1;case 1:return x*3;case 2:return x-7;case 3:return x^5;case 4:return x<<2;case 5:return 9;}return 0;}'
        _,asm=compile_source(src,optimize=2)
        self.assertRegex(asm,r'CJMP = \[J\d+ \+ _cc_jt_\w+\];;\nCJMP \(NP\)\(ABS\)')

    def test_frame_traffic_does_not_wait_for_pointer_accesses(self):
        # Outgoing arguments never alias memory reached through a pointer, so
        # their stores overlap the loads, use a K copy of J27 for balance and
        # the last one shares the CALL's packet.
        _,asm=compile_source('extern int g(int,int,int,int); int f(int*p,int n){return g(p[0],p[1],n,p[2])+1;}',optimize=2)
        self.assertRegex(asm,r'XR\d+ = \[K\d+ \+ \d\]; \[J27 \+ \d\] = XR\d+')
        self.assertRegex(asm,r'CALL _c_g \(NP\)\(NF\); \[[JK]\d+ \+ \d\] = XR\d+')
        self.assertRegex(asm,r'\[K\d+ \+ \d\] = XR\d+')

    def test_no_frame_access_after_the_frame_release(self):
        # No red zone exists below J27: an interrupt pushes there. Restores
        # through a K copy of J27 (and every other frame access) must issue
        # no later than the packet that releases the frame.
        many=('extern int g(int); int f(int*p){int a=p[0],b=p[1],c=p[2],d=p[3],e=p[4],h=p[5],i=p[6],j=p[7],k=p[8],l=p[9];'
              'int r=g(a);return r*a+b*c+d*e+h*i+j*k+l*r+a*b*c;}')
        sources=[many]+[''.join(c[1]) for c in CASES_FOR_FRAME_CHECK]
        for src in sources:
            for level in (1,2):
                try: _,asm=compile_source(src,optimize=level)
                except CompileError: continue
                self.assertEqual(frame_release_violations(asm),[],asm)


def frame_release_violations(asm):
    """Frame accesses ([J27 ...] or through a register copied from J27) in a
    packet after `J27 = J27 + n` within the same block."""
    bad=[];released=None;mirrors=set()
    for line in asm.splitlines():
        s=line.strip()
        if s.endswith(':') and ';' not in s:
            if '_cc_' not in s: mirrors=set()
            released=None; continue
        if not s.endswith(';;'): continue
        bases=re.findall(r'\[\s*([JK]\d+)\s*(?:\+|\])',s)
        if released is not None and any(b=='J27' or b in mirrors for b in bases): bad.append((released,s))
        mirrors|={m for m in re.findall(r'\b([JK]\d+) = J27\b',s) if m!='J27'}
        if re.search(r'\bJ27 = J27 \+ \d+',s): released=s
    return bad


from cases import CASES as _CASES
# Single-unit corpus programs (the multi-unit ones compile per unit elsewhere).
CASES_FOR_FRAME_CHECK=[c for c in _CASES if len(c[1])==1]

if __name__=='__main__':unittest.main()
