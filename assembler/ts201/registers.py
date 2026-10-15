"""Named Ureg facts, independently transcribed from ADI Hardware Reference ch. 2.

No vendor header import. Groups use address bits 16 and 9:5 (HW figure 2-2),
not a byte offset and not the entire low address divided by 32. Table references
are retained per entry. Unlisted aliases/ranges are rejected, not guessed.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Ureg:
    group: int
    index: int
    width: int = 1
    access: str = 'rw'
    reference: str = ''


REGISTERS = {}


def add(name, group, index, width=1, access='rw', reference=''):
    assert name not in REGISTERS and 0<=group<64 and 0<=index<32
    assert width in (1,2,4) and index%width==0 and index+width<=32
    REGISTERS[name]=Ureg(group,index,width,access,reference)


for bank,group in [('J',12),('K',13)]:
    add(bank+'STAT',group,31,reference='PR 7-4')
    for i in range(4):
        add(f'{bank}B{i}',group+2,i,reference='HW 2-25')
        add(f'{bank}L{i}',group+2,i+4,reference='HW 2-25')

for name,index in {'CJMP':0,'RETI':2,'RETS':4,'DBGE':5,'LC0':8,'LC1':9,
                   'IVSW':16,'FLAGREG':20,'FLAGREGST':21,'FLAGREGCL':22,
                   'SQCTL':24,'SQCTLST':25,'SQCTLCL':26,'SQSTAT':27,'SFREG':28}.items():
    access='r' if name=='SQSTAT' else 'w' if name.endswith(('ST','CL')) else 'rw'
    add(name,26,index,access=access,reference='HW table 2-13')

for i in range(3):
    for suffix,index,access in [('CTL',i,'rw'),('STAT',4+i,'r'),('L',8+2*i,'rw'),('H',9+2*i,'rw')]:
        add(f'WP{i}{suffix}',27,index,access=access,reference='HW table 2-29')
for name,index in {'CCNT0':16,'CCNT1':17,'PRFM':18,'PRFCNT':19,'TRCBMASK':20,'TRCBPTR':21,'TRCBVAL':28}.items():
    add(name,27,index,access='r' if name.startswith('TRCB') else 'rw',reference='HW table 2-29')
for i in range(32): add(f'TRCB{i}',10,i,access='r',reference='HW table 2-29')

for block in range(0,12,2):
    group=30+block//8; index=(block%8)*4
    for name,offset,access in [('CACMD',0,'w'),('CCAIR',1,'rw'),('CASTAT',2,'r')]:
        add(f'{name}{block}',group,index+offset,access=access,reference='HW tables 2-14/15; PR 9-20')
    add(f'CACMADR{block}',group,index,2,'w','HW 2-11; PR A-17')
    # PR 8-? stall table: 'CADATAi = <Ureg|imm>', '<Ureg> = CADATAi'; address from Appendix B.
    add(f'CADATA{block}',group,index+3,access='rw',reference='PR B-22 CADATAx_LOC; 8-69 table')
add('CACMDB',31,28,access='w',reference='HW table 2-15')
add('CCAIRB',31,29,access='w',reference='HW table 2-15')
add('CACMADRB',31,28,2,'w','PR A-17')
add('CADATAB',31,31,access='rw',reference='PR B-22 CADATAB_LOC')

# SOC groups have six-bit move selectors, and no constant or memory encoding.
for i in range(4):
    add(f'DCS{i}',32,8*i,4,reference='HW table 2-21')
    add(f'DCD{i}',32,8*i+4,4,reference='HW table 2-21')
for i in range(4,8): add(f'DC{i}',33,(i-4)*4,4,reference='HW table 2-22')
for i in (8,9,10,11,12,13):
    add(f'DC{i}',34,4*(i-8 if i<12 else i-6),4,reference='HW table 2-23')
for name,index,width,access in [('DCNT',0,1,'rw'),('DCNTST',4,1,'w'),('DCNTCL',8,1,'w'),
                                ('DSTAT',12,2,'r'),('DSTATCL',16,2,'r')]:
    add(name,35,index,width,access,'HW table 2-20; HW 2-53')
# Word halves named in PR Appendix B (DEFTS201.H _LOC addresses).
for name,index in [('DSTATL',12),('DSTATH',13),('DSTATCH',17)]:
    add(name,35,index,access='r',reference='PR B-25 DSTATL/H/DSTATCH_LOC')
for name,index in {'SYSCON':0,'BUSLOCK':3,'SDRCON':4,'SYSTAT':6,'SYSTATCL':7,'BMAX':12,'BMAXC':13}.items():
    add(name,36,index,access='r' if name.startswith('SYSTAT') or name=='BMAXC' else 'rw',reference='HW table 2-27')
for i in range(4):
    add(f'LBUFTX{i}',37,8*i,4,'w','HW table 2-25')
    add(f'LBUFRX{i}',37,8*i+4,4,'r','HW table 2-25')
    for name,index,access in [('LRCTL',i,'rw'),('LTCTL',4+i,'rw'),('LRSTAT',16+i,'r'),
                              ('LTSTAT',20+i,'r'),('LRSTATC',24+i,'r'),('LTSTATC',28+i,'r')]:
        add(f'{name}{i}',39,index,access=access,reference='HW table 2-26')

for name,index in {'IVKERNEL':0,'IVTIMER0LP':2,'IVTIMER1LP':3,'IVLINK0':6,'IVLINK1':7,'IVLINK2':8,'IVLINK3':9,
                   'IVDMA0':14,'IVDMA1':15,'IVDMA2':16,'IVDMA3':17,'IVDMA4':22,'IVDMA5':23,
                   'IVDMA6':24,'IVDMA7':25,'IVDMA8':29,'IVDMA9':30,'IVDMA10':31}.items():
    add(name,56,index,reference='HW table 2-17')
for name,index in {'IVDMA11':0,'IVDMA12':5,'IVDMA13':6,'IVIRQ0':9,'IVIRQ1':10,'IVIRQ2':11,
                   'IVIRQ3':12,'VIRPT':16,'IVBUSLK':18,'IVTIMER0HP':20,'IVTIMER1HP':21,'IVHW':25}.items():
    add(name,57,index,reference='HW table 2-18')
add('IVBUSLOCK',57,18,reference='PR B-32 IVBUSLOCK_LOC (HW spelling IVBUSLK)')
for name,index,access in [('ILATL',0,'r'),('ILATH',1,'r'),('ILATSTL',2,'w'),('ILATSTH',3,'w'),
                         ('ILATCLL',4,'w'),('ILATCLH',5,'w'),('PMASKL',6,'r'),('PMASKH',7,'r'),
                         ('IMASKL',8,'rw'),('IMASKH',9,'rw'),('INTCTL',14,'rw')]:
    add(name,58,index,access=access,reference='HW table 2-19; HW 2-46/47')
for i in range(2):
    for half,offset in [('L',0),('H',1)]:
        add(f'TIMER{i}{half}',58,16+2*i+offset,access='r',reference='HW 2-50')
        add(f'TMRIN{i}{half}',58,20+2*i+offset,reference='HW 2-50')


def check(name, kind, writing):
    from .assembler import require
    spec=REGISTERS.get(name.upper())
    if spec is None: return
    require(('w' if writing else 'r') in spec.access,
            f'{name} is {"read-only" if writing else "write-only"} ({spec.reference})')
    require(spec.group<32 or kind=='move','SOC registers require register-to-register moves')
    require(spec.group not in (30,31) or kind in ('move','constant'),
            'cache control/status registers require moves or immediate loads (PR 9-58)')
