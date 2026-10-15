"""Original execution corpus. Expected results are independent integer values."""
CASES = [
 ('arithmetic', ['int main(void) { int x=7; x*=6; return x + 100/7 + 100%7; }'], 58, {}),
 ('signed_division', ['int main(void) { return (-17/5 == -3) + 2*(-17%5 == -2) + 4*(17/-5 == -3) + 8*(17%-5 == 2); }'],15,{}),
 ('unsigned_division', ['unsigned int out[4]; int main(void) { unsigned a=0xffffffffU; out[0]=a/3U; out[1]=a%3U; out[2]=a/0x80000000U; out[3]=a%0x80000000U; return 42; }'],42,{'_c_out':[1431655765,0,1,2147483647]}),
 ('operators',['int main(void) { int a=-64; unsigned b=0x80000000U; return (a>>3)==-8 && (b>>31)==1 && (3<<4)==48 && ~0==-1 && ((10&7)|(12^3))==15; }'],1,{}),
 ('comparisons',['int main(void) { unsigned a=0xffffffffU; return (a>1U) + 2*(a>=a) + 4*(a!=0) + 8*(-2<1) + 16*(-2<=-2) + 32*(0==0); }'],63,{}),
 ('recursion',['int factorial(int n) { if(n<2) return 1; return n*factorial(n-1); } int main(void) { return factorial(7); }'],5040,{}),
 ('nested_calls',['int add(int a,int b,int c,int d,int e,int f) { return a+b+c+d+e+f; } int twice(int x) { return x*2; } int main(void) { return add(twice(1),twice(2),twice(3),twice(4),twice(5),twice(6)); }'],42,{}),
 ('fib',['int fib(int n) { if(n<2)return n; return fib(n-1)+fib(n-2); } int main(void) { return fib(8); }'],21,{}),
 ('loops',['int main(void) { int s=0; for(int i=0;i<10;i++){if(i==3)continue; if(i==8)break;s+=i;} int j=3; do{s+=j;}while(--j); while(s<40)s++;return s; }'],40,{}),
 ('arrays',['int out[4]; int main(void) { int a[4]={3,5,7,9}; int *p=a; for(int i=0;i<4;i++)out[i]=p[i]*2; return (p+3)-p; }'],3,{'_c_out':[6,10,14,18]}),
 ('matrix',['int main(void) { int a[2][3]={{1,2,3},{4,5,6}}; int (*p)[3]=a; return p[1][2] + (p+1-p)*10 + sizeof(a); }'],22,{}),
 ('structs',['struct Pair {int x; int a[2];}; struct Pair g={4,{5,6}}; int main(void) { struct Pair p=g; struct Pair *q=&p; q->a[1]=9; g=p; return sizeof(p)+q->x+g.a[1]; }'],16,{'_c_g':[4,5,9]}),
 ('struct_pointer_difference',['struct P { int a,b,c; }; int main(void) { struct P v[4]; struct P *p=&v[3]; return p-v; }'],3,{}),
 ('short_circuit',['int count; int tick(void){count++;return 1;} int main(void){ int a=0&&tick(); int b=1||tick(); int c=0?tick():40; return a+b+c+count; }'],41,{'_c_count':[0]}),
 ('switch_goto',['int main(void){int s=0; for(int i=0;i<5;i++){switch(i){case 0: s+=1;break;case 1:case 2:s+=2;break;default:if(i==4)goto done;s+=3;}} done:return s;}'],8,{}),
 ('static_local',['int next(void){static int n=10;return n++;} int main(void){return next()+next()+next();}'],33,{}),
 ('bool',['#include <stdbool.h>\nint main(void){bool a=100; bool b=0; b+=3; return a+b+sizeof(bool);}'],3,{}),
 ('headers',['#include <stdint.h>\n#include <stddef.h>\n#include <limits.h>\nint main(void){uint32_t x=UINT32_MAX; size_t n=sizeof(int); return (x+1U==0U)+CHAR_BIT+n+sizeof(char);}'],35,{}),
 ('strings',['char text[]="abc"; char *ptr=text+1; int main(void){char local[]="xy"; return ptr[0]+local[1]+sizeof(text);}'],223,{}),
 ('pointer_relocation',['extern int data[3]; extern int sum(int *p,int n); int *start=data+1; int main(void){return sum(start,2);}', 'int data[3]={1,20,22}; int sum(int *p,int n){int s=0;for(int i=0;i<n;i++)s+=p[i];return s;}'],42,{}),
 ('private_symbols',['static int v=7; static int get(void){return v;} extern int other(void); int main(void){return get()+other();}', 'static int v=35; static int get(void){return v;} int other(void){return get();}'],42,{}),
 ('global_init',['enum N{A=2,B=A*3}; int a[4]={1,B}; int *p=&a[1]; int main(void){return *p+a[2]+a[3]+sizeof(a);}'],10,{'_c_a':[1,6,0,0]}),
 ('precedence',['int main(void){int a=1,b=2,c=3; a+=b*=c; return a*10+b;}'],76,{}),
 ('lvalue_once',['int main(void){int a[3]={2,3,4};int i=0;a[i++] += 5;return a[0]*10+i;}'],71,{}),
 ('const_sizeof',['int main(void){const int k=5;int i=0;return sizeof(i++)+i+k;}'],6,{}),
 ('unsigned_conversion',['int main(void){return (-1<1U)==0 && ((unsigned)-1)==0xffffffffU && ((int)0xffffffffU)==-1;}'],1,{}),
 ('pointer_to_pointer',['int main(void){int a=41;int *p=&a;int **q=&p;**q+=1;return a;}'],42,{}),
 ('void_call',['int n; void set(int x){n=x;} int main(void){set(42);return n;}'],42,{}),
 ('macro',['#define ADD(a,b) ((a)+(b))\n#define N 20\n#if defined(N) && N == 20\nint main(void){return ADD(N,ADD(2,N));}\n#else\n#error wrong branch\n#endif'],42,{}),
 ('array_parameter',['int sum(int a[],int n){return n ? a[n-1]+sum(a,n-1):0;} int main(void){int a[]={10,12,20};return sum(a,3);}'],42,{}),
 ('mutual_recursion',['int odd(int n); int even(int n){return n?odd(n-1):1;} int odd(int n){return n?even(n-1):0;} int main(void){return even(10)*42;}'],42,{}),
 ('integer_boundaries',['int main(void){int a=(-2147483647-1);return (a<2147483647)+2*(a==-2147483647-1)+4*((unsigned)a==0x80000000U);}'],7,{}),
]

REJECTIONS = [
 ('float','float x; int main(void){return 0;}','supported scalar'),
 ('long_long','long long x; int main(void){return 0;}','supported scalar'),
 ('union','union U {int x;int y;}; int main(void){return 0;}','unsupported type'),
 ('variadic','int f(int x,...); int main(void){return 0;}','variadic'),
 ('bitfield','struct S {int x:2;}; int main(void){return 0;}','bitfields'),
 ('function_pointer','int (*f)(int); int main(void){return 0;}','function pointers'),
 ('vla','int main(void){int n=3;int a[n];return 0;}','constant expression'),
 ('undefined','int main(void){return missing;}','undeclared'),
 ('wrong_args','int f(int a){return a;} int main(void){return f();}','number of arguments'),
 ('const_write','int main(void){const int x=1;x=2;return x;}','modifiable'),
 ('const_pointer','int main(void){const int x=1;int *p=&x;return 0;}','discards const'),
 ('void_pointer_arithmetic','int main(void){void *p=0;p++;return 0;}','object size'),
 ('duplicate','int f(void){return 1;} int f(void){return 2;}','duplicate definition'),
 ('break','int main(void){break;return 0;}','break outside'),
 ('goto','int main(void){goto nowhere;}','undefined label'),
 ('array_overflow','int a[2]={1,2,3}; int main(void){return 0;}','too many'),
 ('designator','int a[2]={[1]=3}; int main(void){return 0;}','designated'),
 ('token_paste','#define CAT(a,b) a ## b\nint main(void){return 0;}','token pasting'),
 ('pointer_compound','int main(void){int a=0;int *p=&a;a+=p;return a;}','incompatible'),
 ('nonprototype','int f(); int main(void){return 0;}','prototype'),
]

CASES += [
 ('macro_rescan',['#define SUM(x,y) ((x)+(y))\n#define ALIAS SUM\nint main(void){return ALIAS\n(20,\n22); }'],42,{}),
 ('enum_shadow',['enum E { VALUE=99 }; int main(void){int VALUE=42; return VALUE;}'],42,{}),
 ('struct_const_read',['struct S {const int x;int y;}; int main(void){struct S a={40,2};return a.x+a.y;}'],42,{}),
 ('local_enums',['int first(void){enum X{N=20};return N;} int second(void){enum X{N=22};return N;} int main(void){return first()+second();}'],42,{}),
]
REJECTIONS += [
 ('const_struct_assignment','struct S {const int x;}; int main(void){struct S a={1},b={2};a=b;return 0;}','modifiable'),
 ('enum_shadow_vla','enum E{N=4};int main(void){int N=2;int a[N];return 0;}','constant expression'),
 ('local_enum_scope','int main(void){{enum E{N=42};}return N;}','undeclared'),
]


def division_corpus():
    import random
    rng=random.Random(201)
    for unsigned in (True,False):
        pairs=[(0,1),(1,1),(0xffffffff,1),(0x80000000,1),(0x7fffffff,3),(0xffffffff,0x80000000),
               (0x80000000,0xffffffff),(0x80000000,0x80000000),(0xffffffff,0xffffffff)]
        pairs += [(rng.getrandbits(32),rng.getrandbits(32) or 1) for _ in range(23)]
        for chunk in range(2):
            statements=[]; expected=[]
            for a,b in pairs[chunk*16:(chunk+1)*16]:
                if not unsigned:
                    a=a if a<0x80000000 else a-0x100000000
                    b=b if b<0x80000000 else b-0x100000000
                    if a==-2147483648 and b==-1: b=-3 # signed division overflow is undefined C
                q=abs(a)//abs(b)*(-1 if (a<0)!=(b<0) else 1)
                r=a-q*b
                def c(v):
                    if unsigned: return f'{v}U'
                    if v==-2147483648: return '(-2147483647-1)'
                    return f'({v})'
                offset=len(expected)
                statements += [f'out[{offset}]={c(a)}/{c(b)};',f'out[{offset+1}]={c(a)}%{c(b)};']
                expected += [q&0xffffffff,r&0xffffffff]
            name=f'division_{"unsigned" if unsigned else "signed"}_{chunk}'
            source='unsigned out[32]; int main(void){'+''.join(statements)+'return 42;}'
            yield name,[source],42,{'_c_out':expected}


CASES += list(division_corpus())
CASES += [('relocated_modules',next(c[1] for c in CASES if c[0]=='pointer_relocation'),42,{})]
CASES += [('incomplete_extern_array',['extern int a[]; int main(void){return a[0]+a[1];}','int a[]={20,22};'],42,{})]
CASES += [('c_escapes',[r'''int main(void){char s[]="\x1234" "A"; char t[]="x "; char u[]="\x1" "2"; return sizeof(s)==3 && s[0]==0x1234 && s[1]=='A' && sizeof(t)==3 && t[1]==' ' && u[0]==1 && u[1]=='2' && '\377'==255;}'''],1,{})]


def builtin_memory_case():
    """memcpy/memset expand to loops; sizes come from memory so they are not constants."""
    sizes = [0, 1, 3, 8, 17, 37]
    src = [(i*2654435761 + 7) & 0xffffffff for i in range(64)]
    dst = [0x5a5a5a5a]*128
    pos = 0; mem = list(dst)
    body = []
    for k, n in enumerate(sizes):
        body.append(f'memcpy(dst+{pos}, src+{k}, sizes[{k}]);')
        mem[pos:pos+n] = src[k:k+n]; pos += n + 1
    body.append('memset(dst+100, -3, sizes[4]);'); mem[100:117] = [0xfffffffd]*17
    body.append('memcpy(dst+120, src+50, 5);'); mem[120:125] = src[50:55]
    body.append('memset(dst+126, 9, 2);'); mem[126:128] = [9, 9]
    source = ('extern void *memcpy(void *, const void *, unsigned);\nextern void *memset(void *, int, unsigned);\n'
              'unsigned sizes[6]={' + ','.join(map(str, sizes)) + '};\n'
              'unsigned src[64]={' + ','.join(f'{v}U' for v in src) + '};\n'
              'unsigned dst[128]={' + ','.join(f'{v}U' for v in dst) + '};\n'
              'int main(void){' + ''.join(body) + 'return 42;}')
    return ('builtin_memory', [source], 42, {'_c_dst': mem})


CASES += [builtin_memory_case()]
CASES += [('builtin_ctype',['extern int toupper(int); extern int tolower(int);\nint in[8]={96,97,122,123,64,65,90,-1}; int up[8]; int down[8];\nint main(void){int i;for(i=0;i<8;i++){up[i]=toupper(in[i]);down[i]=tolower(in[i]);}return up[1]+down[5];}'],65+97,
           {'_c_up':[96,65,90,123,64,65,90,0xffffffff],'_c_down':[96,97,122,123,64,97,122,0xffffffff]})]


def builtin_memory_quad_case():
    """Quad-word memcpy fast path: every size 8..40 at each source/destination alignment."""
    src = [(i*2246822519 + 3) & 0xffffffff for i in range(64)]
    mem = [0]*(16*8)
    lines = []
    for k, (n, so, do) in enumerate([(n, n % 4, (n // 4) % 4) for n in range(8, 41)]):
        dst = (k % 16) * 8
        lines.append(f'memcpy(dst+{dst}+{do}, src+{so}, sizes[{k}]);')
    # Replay the copies in order to build the expected image.
    out = [0]*(16*8+48)
    ops = [(n, n % 4, (n // 4) % 4) for n in range(8, 41)]
    for k, (n, so, do) in enumerate(ops):
        dst = (k % 16) * 8
        out[dst+do:dst+do+n] = src[so:so+n]
    source = ('extern void *memcpy(void *, const void *, unsigned);\n'
              f'unsigned sizes[{len(ops)}]={{' + ','.join(str(n) for n, _, _ in ops) + '};\n'
              'unsigned src[64]={' + ','.join(f'{v}U' for v in src) + '};\n'
              f'unsigned dst[{len(out)}];\n'
              'int main(void){' + ''.join(lines) + 'return 42;}')
    return ('builtin_memory_quad', [source], 42, {'_c_dst': out})


CASES += [builtin_memory_quad_case()]

# Tail calls: arguments land in the caller's incoming slots (reordered, more
# arguments than parameters within the rounded area, void, self-recursive).
# deep(2000) needs a flat stack: 2000 nested frames would overrun the 4096-word
# stack guard. `addr` passes a local's address, so its call must stay a call.
CASES += [('tail_calls', [
    'extern int pick(int a,int b,int c,int d); extern void store(int v); extern int total;\n'
    'extern int rd(int *p,int a,int b,int c);\n'
    'int swap(int x,int y,int z){return pick(y,x,z,x+y);}\n'
    'int deep(int n,int acc){if(n==0)return acc;return deep(n-1,acc+n);}\n'
    'void put(int v){store(v*2);}\n'
    'int addr(int v){int t=v+1;return rd(&t,v,0,0);}\n'
    'int main(void){put(5);return swap(1,2,3)+deep(2000,0)+addr(20)+total;}',
    'int total; int pick(int a,int b,int c,int d){return a*1000+b*100+c*10+d;}\n'
    'void store(int v){total+=v;} int rd(int *p,int a,int b,int c){return *p+a+b+c;}'],
    2133 + 2001000 + 41 + 10, {'_c_total': [10]})]

# Branch packets carry fall-through work as ELSE operations: both directions
# of each branch must see exactly the effects of the path taken.
CASES += [('branch_else', [
    'int a[8]={1,9,3,4,0,12,7,2}; int out[8]; int s;\n'
    'int f(int x,int y){int r; if(x<y){r=x+3; s+=r;} else r=y*7; return r+x;}\n'
    'int main(void){int i,t=0; for(i=0;i<8;i++){if(a[i]<4) out[i]=a[i]+3; else out[i]=a[i]*7; t+=out[i];}\n'
    'return t+f(1,2)+f(5,2)*100+s;}'],
    4+63+6+28+3+84+49+5 + 5 + 19*100 + 4, {'_c_out': [4, 63, 6, 28, 3, 84, 49, 5], '_c_s': [4]})]

# Shrink-wrapped early exits: the fast path runs on the caller's J27 with
# arguments moved to scratch registers; the slow path reloads them after
# saving. Conditional returns (`IF cond, CJMP`) on both.
CASES += [('early_exit', [
    'extern int active; extern int work(int); extern int calls;\n'
    'int f(int x,int y){int a; if(x>y) return y; a=work(x); a+=work(y+a); return a*x;}\n'
    'int g(int n){if(!active) return n; return work(n)+work(n+1);}\n'
    'int main(void){int t=f(9,4)+f(2,5)*10; t+=g(3); active=1; t+=g(3)*1000; return t+calls*100000;}',
    'int active; int calls; int work(int v){calls++; return v*2;}'],
    4 + 44*10 + 3 + 14*1000 + 4*100000, {'_c_calls': [4]})]
