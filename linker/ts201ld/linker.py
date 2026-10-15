"""Relocatable symbolic packets; the existing assembler owns final encodings.

All layout and relocation arithmetic uses DSP words. No host byte addresses,
vendor libraries, executable serialization or undocumented binary patching.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assembler'))
from ts201.assembler import assemble, packet, expression, strip_comments, AssemblyError, BANKS
from ts201.packet_rules import effects

FORMAT = 'ts201-symbolic-object-v1'
ABI = 'ts201-word32-stack-v1'
IDENT = r'[A-Za-z_][A-Za-z_0-9]*'
TOKEN_IDENT = r'(?<![A-Za-z_0-9])' + IDENT + r'(?![A-Za-z_0-9])'


class LinkError(ValueError):
    pass


def check(value, message):
    if not value:
        raise LinkError(message)


def make_object(source, name='<assembly>', types=None):
    """Parse relocatable assembly with .global/.extern and address-free sections."""
    sections, definitions, exports, imports = [], {}, set(), set()
    current = None
    for number, raw in enumerate(strip_comments(source).splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            if line.startswith(('.global ', '.extern ')):
                directive, names = line.split(None, 1)
                for symbol in names.split(','):
                    symbol = symbol.strip()
                    check(re.fullmatch(IDENT, symbol), 'invalid symbol')
                    (exports if directive == '.global' else imports).add(symbol)
                continue
            if line.startswith('.section '):
                parts = [x.strip() for x in line[9:].split(',')]
                check(len(parts) in (2, 3), '.section requires name, code|data [, alignment]')
                section_name, kind = parts[:2]
                alignment = expression(parts[2]) if len(parts) == 3 else 4
                check(re.fullmatch(IDENT, section_name), 'invalid section name')
                check(kind in ('code', 'data'), 'invalid section kind')
                check(1 <= alignment <= 0x20000 and alignment & (alignment-1) == 0,
                      'section alignment must be a power of two in words')
                check(not any(s['name'] == section_name for s in sections), 'duplicate section')
                current = dict(name=section_name, kind=kind, alignment=alignment, lines=[])
                sections.append(current)
                continue
            check(current is not None, 'content before section')
            label = re.match(r'(' + IDENT + r'):(?![0-9])\s*', line)
            if label:
                symbol = label[1]
                check(symbol not in definitions, f'duplicate symbol: {symbol}')
                definitions[symbol] = current['kind']
                current['lines'].append(symbol + ':')
                line = line[label.end():].strip()
            if not line:
                continue
            if line.startswith('.align '):
                alignment = expression(line[7:])
                check(1 <= alignment <= 0x20000 and alignment & (alignment-1) == 0,
                      'alignment must be a power of two in words')
                current['alignment'] = max(current['alignment'], alignment)
            elif line.startswith('.word '):
                check(current['kind'] == 'data', '.word requires data section')
                for v in line[6:].split(','):
                    # Syntax only: the assembler checks each final value.
                    from ts201.assembler import has_symbol
                    has_symbol(v)
            elif line.startswith('.zero '):
                check(current['kind'] == 'data', '.zero requires data section')
                check(1 <= expression(line[6:]) <= 0x20000, 'invalid zero count')
            else:
                check(current['kind'] == 'code' and line.endswith(';;'), 'expected instruction packet')
                packet(line[:-2], layout=True)
            current['lines'].append(line)
        except (LinkError, AssemblyError) as e:
            raise LinkError(f'{name}:{number}: {e}') from None
    check(sections, f'{name}: no sections')
    check(not (imports & definitions.keys()), f'{name}: symbol is both imported and defined')
    check(exports <= definitions.keys(), f'{name}: undefined export: {sorted(exports-definitions.keys())}')
    all_names = definitions.keys() | imports
    dummy_symbols = dict.fromkeys(all_names, 0)
    relocations = []
    for si, s in enumerate(sections):
        check(section_size(s) > 0, f'{name}: empty section {s["name"]}')
        for li, line in enumerate(s['lines']):
            if line.endswith(':'):
                continue
            try:
                if line.startswith('.word '):
                    for value in line[6:].split(','): expression(value, dummy_symbols)
                elif not line.startswith('.'):
                    packet(line[:-2], dummy_symbols)
            except AssemblyError as e:
                raise LinkError(f'{name}:{s["name"]}:{li+1}: {e}') from None
            for symbol in sorted(set(re.findall(TOKEN_IDENT, line)) & all_names):
                relocations.append(dict(section=si, line=li, symbol=symbol,
                                        kind='WORD_EXPR' if s['kind']=='data' else 'PACKET_EXPR'))
    return dict(format=FORMAT, abi=ABI, name=Path(name).name, sections=sections,
                exports={n: definitions[n] for n in sorted(exports)}, imports=sorted(imports),
                symbols=definitions, types=types or {}, relocations=relocations)


def section_size(section):
    size = 0
    for line in section['lines']:
        if line.endswith(':'):
            continue
        if line.startswith('.align '):
            size += (-size) % expression(line[7:])
        elif line.startswith('.word '):
            size += len(line[6:].split(','))
        elif line.startswith('.zero '):
            size += expression(line[6:])
        else:
            size += len(packet(line[:-2], layout=True)[0])
        check(size <= 0x4000000, 'section exceeds largest TS201 memory window')
    return size


def validate_object(obj):
    check(isinstance(obj, dict) and obj.get('format') == FORMAT, 'unsupported object format')
    check(obj.get('abi') == ABI, 'incompatible object ABI')
    try:
        check(isinstance(obj.get('types', {}), dict), 'object types must be a dictionary')
        check(set(obj.get('types', {})) <= set(obj['exports']) | set(obj['imports']),
              'type metadata must describe an export or import')
        for value in obj.get('types', {}).values(): validate_signature(value)
        text = ['.global '+n for n in obj['exports']] + ['.extern '+n for n in obj['imports']]
        for s in obj['sections']:
            text.append(f'.section {s["name"]}, {s["kind"]}, {s["alignment"]}')
            check(all(isinstance(l, str) and '\n' not in l and '\r' not in l for l in s['lines']),
                  'object lines must be single lines')
            text += s['lines']
        canonical = make_object('\n'.join(text), obj['name'], obj.get('types', {}))
        for key in ('sections','exports','imports','symbols','relocations'):
            check(canonical[key] == obj[key], f'inconsistent object {key}')
        return canonical
    except (KeyError, TypeError, AttributeError) as e:
        raise LinkError(f'malformed object: {e}') from None


def write_object(obj, path):
    validate_object(obj)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True)+'\n', encoding='utf-8')


def read_object(path):
    try:
        return validate_object(json.loads(Path(path).read_text(encoding='utf-8')))
    except (OSError, ValueError) as e:
        raise LinkError(f'{path}: {e}') from None


def merge_signature(a,b,name):
    """C permits an extern incomplete array to match a complete definition."""
    if a==b: return a
    if isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
        if len(a)==3 and a[0]==b[0]=='array':
            check(a[1]==b[1] or a[1]==0 or b[1]==0,f'incompatible declarations for {name}')
            return ['array',a[1] or b[1],merge_signature(a[2],b[2],name)]
        return [merge_signature(x,y,name) for x,y in zip(a,b)]
    raise LinkError(f'incompatible declarations for {name}')


def validate_signature(t,depth=0):
    check(depth<64 and isinstance(t,list) and len(t)>0 and isinstance(t[0],str),'malformed type signature')
    tag=t[0]
    if tag in ('void','bool'): check(len(t)==1,'malformed scalar signature')
    elif tag=='int': check(len(t)==2 and type(t[1]) is bool,'malformed integer signature')
    elif tag=='ptr':
        check(len(t)==3 and type(t[2]) is bool,'malformed pointer signature'); validate_signature(t[1],depth+1)
    elif tag=='array':
        check(len(t)==3 and type(t[1]) is int and 0<=t[1]<=0x20000,'malformed array signature'); validate_signature(t[2],depth+1)
    elif tag=='function':
        check(len(t)==3 and isinstance(t[2],list),'malformed function signature')
        validate_signature(t[1],depth+1)
        for p in t[2]: validate_signature(p,depth+1)
    elif tag=='struct-ref': check(len(t)==2 and isinstance(t[1],str),'malformed struct reference')
    elif tag=='struct':
        check(len(t)==3 and isinstance(t[1],str) and isinstance(t[2],list),'malformed struct signature')
        for f in t[2]:
            check(isinstance(f,list) and len(f)==2 and isinstance(f[0],str),'malformed field signature')
            validate_signature(f[1],depth+1)
    else: raise LinkError('unknown type signature kind')


SEQUENCER_KINDS = {'branch', 'indirect', 'reduce', 'condition', 'flag', 'misc'}
# Ureg groups an absorbed operation may write: X/Y/XY compute data and J/K
# (never JSTAT/KSTAT, circular-buffer, sequencer, debug or SOC registers).
DATA_GROUPS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13}


def layout_items(lines):
    """Size each line once. Items: [kind, text, value, ops]; value is a
    label name, alignment or word count. Packets are sized as the layout pass
    of the assembler sizes them (symbolic operands keep their extensions)."""
    items = []
    for line in lines:
        if line.endswith(':'):
            items.append(['label', line, line[:-1], None])
        elif line.startswith('.align '):
            items.append(['align', line, expression(line[7:]), None])
        elif line.startswith('.word '):
            items.append(['words', line, len(line[6:].split(',')), None])
        elif line.startswith('.zero '):
            items.append(['words', line, expression(line[6:]), None])
        else:
            words, ops = packet(line[:-2], layout=True)
            items.append(['packet', line, len(words), ops])
    return items


def place(sections, cursors, short):
    """Assign addresses: returns symbols; sets each section's start, size and
    per-item addresses. `short` holds (section, item) packets whose branch
    uses the one-slot form, one word smaller than its layout size."""
    symbols, cursors = {}, dict(cursors)
    for si, s in enumerate(sections):
        alignment = s['alignment']
        base = s['fixed'] if s.get('fixed') is not None else (cursors[s['kind']]+alignment-1) & -alignment
        at, addresses = base, []
        for li, (kind, _, value, _) in enumerate(s['items']):
            addresses.append(at)
            if kind == 'label':
                symbols[value] = at
            elif kind == 'align':
                at += (-at) % value
            elif kind == 'words':
                at += value
            else:
                at += value - ((si, li) in short)
        s['start'], s['size'], s['addresses'] = base, at-base, addresses
        cursors[s['kind']] = at
    return symbols


def relative_branch(ops):
    op = ops[0]
    return op if op.kind == 'branch' and not op.absolute else None


def short_form(text):
    """Request the one-slot 15-bit branch: the branch is the first operation."""
    at = text.index(';')
    return text[:at] + ' (S)' + text[at:]


def fold_branch_targets(sections):
    """Copy the first packet of a branch target into the line of an
    unconditional JUMP or CALL to it; the branch then continues after the
    copied packet (a call enters its callee one packet later).

    The copied operations run in the branch line instead of the next line
    executed. That keeps the program's meaning when they read nothing the
    branch line writes (a CALL writes CJMP), write nothing it writes, and
    touch no memory the line also touches unless both use one base register
    with disjoint literal offsets. The copy must write only compute or J/K
    registers, hold no sequencer operation and pass the assembler's packet
    rules. Each fold saves one executed packet per pass and costs the copied
    words. Applied only to compiler output (objects named *.c, never
    assembly, whose timing and layout may be deliberate).
    Returns {(section, item): original item}."""
    targets = {}
    for si, s in enumerate(sections):
        pending = []
        for li, item in enumerate(s['items']):
            if item[0] == 'label':
                pending.append(item[2])
                continue
            for name in pending:
                targets[name] = (si, li) if item[0] == 'packet' else None
            pending = []
    changed = {}
    for si, s in enumerate(sections):
        if not s['compiled']:
            continue
        for li, item in enumerate(s['items']):
            if item[0] != 'packet' or not relative_branch(item[3]):
                continue
            parts = [part.strip() for part in item[1][:-2].split(';')]
            jump = re.fullmatch(r'(JUMP|CALL)\s+(' + IDENT + r')\s*((?:\(\s*(?:NP|BP|NF|S)\s*\)\s*)*)', parts[0], re.I)
            where = jump and targets.get(jump[2])
            if not where or not sections[where[0]]['compiled'] or len(parts) != len(item[3]):
                continue
            titems = sections[where[0]]['items']
            _, ttext, tsize, tops = titems[where[1]]
            following = next((x for x in titems[where[1]+1:] if x[0] != 'label'), None)
            if following is None or following[0] != 'packet':
                continue
            nf = foldable(item[3], tops)
            if nf is None:
                continue
            options = [o.upper() for o in re.findall(r'\(\s*(\w+)\s*\)', jump[3]) if o.upper() != 'NF']
            text = '; '.join([f'{jump[1].upper()} {jump[2]} + {tsize} ' + ''.join(f'({o})' for o in options + ['NF'] * nf)]
                             + parts[1:] + [ttext[:-2]]) + ';;'
            try:
                words, ops = packet(text[:-2], layout=True)
            except AssemblyError:
                continue
            changed[si, li] = item
            s['items'][li] = ['packet', text, len(words), ops]
    return changed


def foldable(line, copy):
    """Line NF for folding the operations `copy` into branch line `line`
    (decoded packets), or None when that could change the program."""
    if (line[0].words[0] >> 16) & 63 != 8 or line[0].words[0] & 1 << 22 or not all(absorbable(op) for op in copy):
        return None
    capable = lambda ops: [op for op in ops if op.kind in ('alu', 'compute') and op.nf_ok]
    line_nf = bool(line[0].words[0] & 1 << 15)
    copy_nf = {bool(op.words[0] & 1 << 30) for op in capable(copy)}
    if len(copy_nf) > 1 or capable(line[1:]) and copy_nf and copy_nf != {line_nf}:
        return None
    nf = line_nf if capable(line[1:]) or not copy_nf else copy_nf.pop()
    written, accesses = set(), []
    for op in line:
        effect = effects(op)
        if effect is None:
            return None
        written |= effect[1]
        accesses += effect[2]
    for op in copy:
        reads, writes, touches = effects(op)
        if reads & written or writes & written:
            return None
        for base, offset, width, store in touches:
            for other_base, other_offset, other_width, _ in accesses:
                if (base != other_base or offset is None or other_offset is None
                        or offset < other_offset + other_width and other_offset < offset + width):
                    return None
    return nf


def absorbable(op):
    if op.kind in SEQUENCER_KINDS:
        return False
    w = op.words[0]
    if op.kind == 'constant':
        group, index = (w >> 21) & 31, (w >> 16) & 31
    elif op.kind == 'move':
        group, index = (w >> 8) & 63, (w >> 1) & 31
    elif op.kind == 'memory' and not w & 1 << 26:
        group, index = (w >> 21) & 31, (w >> 16) & 31
    else:
        return op.kind in ('compute', 'alu', 'memory', 'nop')
    return group in DATA_GROUPS and not (group in (12, 13) and index == 31)


def relax(sections, cursors, *, shorten=True, fold=True):
    """Shrink branches to the one-slot form where the placed target is in
    range, and fold jump targets (fold_branch_targets). Iterates to a fixpoint:
    a packet that stops fitting returns to its original form for good."""
    folded = fold_branch_targets(sections) if fold else {}
    short = set()
    if shorten:
        for si, s in enumerate(sections):
            for li, item in enumerate(s['items']):
                op = item[0] == 'packet' and relative_branch(item[3])
                if op and len(op.words) == 2:
                    short.add((si, li))
    while True:
        symbols = place(sections, cursors, short)
        bad = set()
        for si, li in short | set(folded):
            item, pc = sections[si]['items'][li], sections[si]['addresses'][li]
            op = relative_branch(item[3])
            if (si, li) in short or len(op.words) == 1:
                if not -0x4000 <= expression(op.target, symbols) - pc < 0x4000:
                    bad.add((si, li))
        if not bad:
            # A predicted branch's line must not end in the quad of another's
            # (the assembler's rule); undo shrinking near any new collision.
            ends = {}
            for si, s in enumerate(sections):
                for li, item in enumerate(s['items']):
                    if item[0] == 'packet' and any(op.predicted for op in item[3]):
                        end = s['addresses'][li] + item[2] - ((si, li) in short) - 1
                        ends.setdefault(end // 4, []).append((si, li))
            clash = {key for group in ends.values() if len(group) > 1 for key in group}
            bad = (clash & short or {k for k in short if k[0] in {c[0] for c in clash}}
                   or (short | set(folded) if clash else set()))
        if not bad:
            return symbols, short
        for key in bad & set(folded):
            sections[key[0]]['items'][key[1]] = folded.pop(key)
        short -= bad


def terminal(ops):
    """The packet never falls through: an unconditional JUMP, CJMP, RTI or RETI."""
    op = ops[0]
    if op.kind not in ('branch', 'indirect'):
        return False
    w = op.words[0]
    call = w & 1 << 23 if op.kind == 'branch' else w & 1 << 5
    return (w >> 16) & 63 == 8 and not w & 1 << 22 and not call


def collect_garbage(sections, roots):
    """Drop code and data that no root reaches through symbol references.

    Sections of compiled objects (*.c) split into chunks: code at a label or
    alignment that no packet falls into (the previous packet is terminal),
    data at every label. Other sections, including all assembly, stay whole.
    A chunk is live if the startup code or a root is in it, or a live chunk
    names one of its labels. Surviving compiled data keeps its offset modulo
    four within its section, so quad-aligned objects stay quad aligned."""
    chunks = []
    for si, s in enumerate(sections):
        items = s['items']
        if s['name'] == 'startup' or not s['compiled'] or s['kind'] == 'data' and any(i[0] == 'align' for i in items):
            chunks.append([si, 0, len(items)])
            continue
        start, content, falls = 0, False, True
        for li, (kind, _, _, ops) in enumerate(items):
            if kind in ('label', 'align') and content and (s['kind'] == 'data' or not falls):
                chunks.append([si, start, li])
                start, content = li, False
            if kind == 'packet':
                content, falls = True, not terminal(ops)
            elif kind == 'words':
                content = True
        chunks.append([si, start, len(items)])
    owner = {}
    for ci, (si, a, b) in enumerate(chunks):
        for kind, _, value, _ in sections[si]['items'][a:b]:
            if kind == 'label':
                owner[value] = ci
    live = {ci for ci, (si, _, _) in enumerate(chunks) if sections[si]['name'] == 'startup'}
    live |= {owner[name] for name in roots if name in owner}
    work = list(live)
    while work:
        si, a, b = chunks[work.pop()]
        for kind, text, _, _ in sections[si]['items'][a:b]:
            if kind == 'label':
                continue
            for name in re.findall(TOKEN_IDENT, text):
                ci = owner.get(name)
                if ci is not None and ci not in live:
                    live.add(ci)
                    work.append(ci)
    kept, discarded = [], dict(code_words=0, data_words=0, symbols=[])
    for si, s in enumerate(sections):
        items, offset, new_offset = [], 0, 0
        for ci, (csi, a, b) in enumerate(chunks):
            if csi != si:
                continue
            part = s['items'][a:b]
            size = sum(value for kind, _, value, _ in part if kind in ('words', 'packet'))
            if ci in live:
                pad = (offset - new_offset) % 4 if s['kind'] == 'data' else 0
                if pad:
                    items.append(['words', f'.zero {pad}', pad, None])
                items += part
                new_offset += pad + size
            else:
                discarded[s['kind'] + '_words'] += size
                discarded['symbols'] += [value for kind, _, value, _ in part if kind == 'label']
            offset += size
        if any(kind in ('words', 'packet') for kind, _, _, _ in items):
            kept.append(dict(s, items=items))
    discarded['symbols'].sort()
    return kept, discarded


def link(objects, *, entry='_c_main', code_base=0x100, data_base=0x40000,
         stack_base=0x140000, stack_words=4096, startup=True, memory_windows=None,
         relax_branches=True, fold_branches=True, keep=None):
    """keep=None links everything; a collection of exported names (possibly
    empty) enables garbage collection rooted at the entry and those names."""
    windows = BANKS if memory_windows is None else tuple(memory_windows)
    objects = [validate_object(o) for o in objects]
    check(objects, 'no input objects')
    exports, signatures = {}, {}
    for i, obj in enumerate(objects):
        for name, kind in obj['exports'].items():
            check(name not in ('_start','_halt','_stack_low','_stack_high') and not re.match(r'_m\d+_',name),
                  f'export uses reserved linker symbol: {name}')
            check(name not in exports, f'duplicate export: {name}')
            exports[name] = (i, kind)
        for name, typ in obj['types'].items():
            signatures[name] = merge_signature(signatures[name],typ,name) if name in signatures else typ
    for obj in objects:
        for name in obj['imports']:
            check(name in exports, f'{obj["name"]}: unresolved import: {name}')
    for name,typ in signatures.items():
        if name in exports:
            check(exports[name][1]==('code' if typ[0]=='function' else 'data'),
                  f'code/data symbol kind mismatch: {name}')
    check(entry in exports and exports[entry][1] == 'code', f'entry is not an exported function: {entry}')
    if startup and entry in signatures:
        check(signatures[entry] == ['function', ['int', False], []],
              'startup entry must have signature int entry(void)')
    mappings = []
    for i, obj in enumerate(objects):
        mapping = {name: f'_m{i}_{name}' for name in obj['symbols']}
        mapping.update({name: f'_m{exports[name][0]}_{name}' for name in obj['imports']})
        mappings.append(mapping)
    resolved_entry = mappings[exports[entry][0]][entry]
    check(type(stack_words) is int and stack_words >= 16 and stack_words % 4 == 0,
          'stack size must be a multiple of four, at least 16 words')
    check(stack_base % 4 == 0, 'stack base must be quad aligned')
    sections, placements = [], []
    if startup:
        startup_lines = ['_start:', f'J27 = {stack_base+stack_words-4};;', 'J26 = 0;;',
                         f'CALL {resolved_entry} (NP);;', '_halt:', 'IDLE;;']
        sections.append(dict(name='startup', kind='code', alignment=1, fixed=code_base, compiled=False,
                             items=layout_items(startup_lines)))
    for i, obj in enumerate(objects):
        mapping = mappings[i]
        for si, s in enumerate(obj['sections']):
            renamed = [re.sub(TOKEN_IDENT, lambda m: mapping.get(m[0], m[0]), line) for line in s['lines']]
            sections.append(dict(name=f'm{i}_s{si}', kind=s['kind'], alignment=s['alignment'],
                                 object=obj['name'], section=s['name'],
                                 compiled=obj['name'].lower().endswith('.c'),
                                 items=layout_items(renamed)))
    discarded = {}
    if keep is not None:
        check(set(keep) <= exports.keys(), f'kept names are not exported: {sorted(set(keep)-exports.keys())}')
        roots = {resolved_entry} | {mappings[exports[n][0]][n] for n in keep}
        sections, discarded = collect_garbage(sections, roots)
    try:
        planned, short = relax(sections, dict(code=code_base, data=data_base),
                               shorten=relax_branches, fold=fold_branches)
    except AssemblyError as e:
        raise LinkError(str(e)) from None
    lines = ['.entry _start'] if startup else [f'.entry {resolved_entry}']
    for si, s in enumerate(sections):
        base, count = s['start'], s['size']
        if s['name'] != 'startup':
            check(any(lo <= base and base+count <= hi for lo, hi in windows),
                  f'{s["object"]}:{s["section"]}: placement crosses bank or reserved memory')
            placements.append(dict(object=s['object'], section=s['section'], kind=s['kind'],
                                   start_word_address=base, words=count, alignment=s['alignment']))
        lines.append(f'.section {s["name"]}, {s["kind"]}, {base}')
        lines += [short_form(item[1]) if (si, li) in short else item[1] for li, item in enumerate(s['items'])]
    if startup:
        lines += [f'.section stack, data, {stack_base}', '_stack_low: .word 0x51acce55',
                  f'.zero {stack_words-5}', '_stack_high: .word 0x51acce55, 0x51acce55, 0x51acce55, 0x51acce55']
    source = '\n'.join(lines)+'\n'
    try:
        image = assemble(source, '<linked>', memory_windows=windows)
    except AssemblyError as e:
        raise LinkError(str(e)) from None
    # The linker's placement must be the assembler's; anything else is a bug.
    check(all(image.symbols.get(name) == address for name, address in planned.items()),
          'internal error: linker placement disagrees with the assembler')
    # Public aliases appear in maps only, avoiding assembler/register collisions.
    for name, (index, _) in exports.items():
        if mappings[index][name] in image.symbols:
            image.symbols[name] = image.symbols[mappings[index][name]]
    report = dict(format='ts201-link-map-v1', abi=ABI, address_unit='32-bit DSP word',
                  entry=entry, placements=placements,
                  exports={n:image.symbols[n] for n in sorted(exports) if n in image.symbols},
                  objects=[dict(name=o['name'], sha256=hashlib.sha256(
                      json.dumps(o, sort_keys=True).encode()).hexdigest()) for o in objects])
    if keep is not None:
        report['discarded'] = discarded
    return image, source, report


def write_image(result, directory):
    image, source, report = result
    image.write(directory)
    directory = Path(directory)
    (directory/'linked.asm').write_text(source, encoding='utf-8')
    (directory/'link-map.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')

