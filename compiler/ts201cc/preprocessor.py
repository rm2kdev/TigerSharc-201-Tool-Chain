"""Small deterministic C preprocessor. Unsupported directives fail explicitly."""
from pathlib import Path
import json
import re
from pycparser import c_parser


class PreprocessError(ValueError):
    pass


TOKEN = re.compile(r'''(?:L)?"(?:\\.|[^"\\])*"|(?:L)?'(?:\\.|[^'\\])*'|[A-Za-z_]\w*|(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*|##|<<=|>>=|[+*/%&|^=-]=|<<|>>|&&|\|\||==|!=|<=|>=|\+\+|--|->|\n|[^\s]''')


def remove_comments(text):
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*', re.S)
    text = pattern.sub(lambda m: (' ' + '\n'*m[0].count('\n')) if m[0].startswith(('/',)) else m[0], text)
    if '/*' in re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '', text):
        raise PreprocessError('unterminated comment')
    return text


class Preprocessor:
    def __init__(self, includes=(), defines=()):
        self.includes = [Path(p) for p in includes] + [Path(__file__).resolve().parents[1]/'include']
        self.macros = {'__TS201__': (None,['1']), '__STDC_HOSTED__': (None,['0']), '__CHAR_BIT__': (None,['32'])}
        for d in defines:
            name, _, value = d.partition('=')
            self.macros[name] = (None, TOKEN.findall(value or '1'))
        self.dependencies = []

    def expand(self, tokens, disabled=frozenset(), depth=0):
        """Rescan replacements with per-token hide sets (including boundary calls)."""
        if depth > 100:
            raise PreprocessError('macro expansion nesting limit exceeded')
        stream=[(t,disabled) for t in tokens]
        out, i, expansions = [], 0, 0
        while i < len(stream):
            name,hidden = stream[i]
            if name not in self.macros or name in hidden:
                out.append(name); i += 1; continue
            params, body = self.macros[name]
            if params is None:
                stream[i:i+1]=[(t,hidden|{name}) for t in body]
                expansions+=1
                if expansions>10000: raise PreprocessError('macro expansion limit exceeded')
                continue
            opening=i+1
            while opening<len(stream) and stream[opening][0]=='\n': opening+=1
            if opening >= len(stream) or stream[opening][0] != '(':
                out.append(name); i += 1; continue
            args, arg, nesting, j = [], [], 0, opening+1
            while j < len(stream):
                t = stream[j][0]
                if t == ')' and nesting == 0:
                    if arg or args or params: args.append(arg)
                    break
                if t == ',' and nesting == 0:
                    args.append(arg); arg=[]
                elif t!='\n':
                    arg.append(t)
                    nesting += (t=='(') - (t==')')
                j += 1
            if j == len(stream) or len(args) != len(params):
                raise PreprocessError(f'invalid arguments for macro {name}')
            values = {p:self.expand(a, disabled, depth+1) for p,a in zip(params,args)}
            replaced = [v for t in body for v in values.get(t,[t])]
            replaced+=['\n']*sum(t=='\n' for t,_ in stream[i:j+1])
            stream[i:j+1]=[(t,hidden|{name}) for t in replaced]
            expansions+=1
            if expansions>10000: raise PreprocessError('macro expansion limit exceeded')
        return out

    def condition(self, text):
        text = re.sub(r'\bdefined\s*(?:\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))',
                      lambda m:str(int((m[1] or m[2]) in self.macros)), text)
        tokens = self.expand(TOKEN.findall(text))
        tokens = ['0' if re.fullmatch(r'[A-Za-z_]\w*', t) else t for t in tokens]
        from .compiler import const_int
        try:
            ast = c_parser.CParser().parse('int x = '+ ' '.join(tokens)+';')
            return const_int(ast.ext[0].init, {}, preprocessor=True) != 0
        except Exception as e:
            raise PreprocessError(f'invalid #if expression: {e}') from None

    def process(self, source, filename='<input>', depth=0):
        if depth > 64:
            raise PreprocessError('include nesting limit exceeded')
        source = remove_comments(source.replace('\\\r\n','').replace('\\\n',''))
        output, pending, conditions = [], [], []
        pending_start=1
        active = True
        def flush():
            if pending:
                output.append('#line '+str(pending_start)+' '+json.dumps(str(filename).replace('\\','/')))
                output.append(' '.join(self.expand(TOKEN.findall('\n'.join(pending)))))
                pending.clear()
        for number, line in enumerate(source.splitlines(), 1):
            try:
                if not line.lstrip().startswith('#'):
                    if active:
                        # Keep ordinary source coordinates for the parser where possible.
                        if not pending: pending_start=number
                        pending.append(line)
                    continue
                flush()
                m = re.match(r'\s*#\s*(\w*)(.*)', line)
                command, value = m[1], m[2].strip()
                if command in ('if','ifdef','ifndef'):
                    test = (value in self.macros) if command=='ifdef' else (value not in self.macros) if command=='ifndef' else self.condition(value) if active else False
                    conditions.append([active, bool(test), False])
                    active = active and bool(test)
                elif command in ('elif','else'):
                    if not conditions or conditions[-1][2]:
                        raise PreprocessError('unmatched or repeated #else/#elif')
                    parent, taken, _ = conditions[-1]
                    test = not taken and (command=='else' or (self.condition(value) if parent else False))
                    active = parent and test
                    conditions[-1][1] |= bool(test)
                    conditions[-1][2] = command=='else'
                elif command == 'endif':
                    if not conditions: raise PreprocessError('unmatched #endif')
                    active = conditions.pop()[0]
                elif not active:
                    continue
                elif command == 'define':
                    definition = re.fullmatch(r'([A-Za-z_]\w*)(\(([^)]*)\))?(.*)', value)
                    if not definition: raise PreprocessError('invalid #define')
                    name, parens, parameters, body = definition.groups()
                    params = [p.strip() for p in parameters.split(',') if p.strip()] if parens else None
                    if params is not None and (len(set(params)) != len(params) or any(not re.fullmatch(r'[A-Za-z_]\w*', p) for p in params)):
                        raise PreprocessError('variadic/invalid macro parameters unsupported')
                    tokens = TOKEN.findall(body)
                    if '#' in tokens or '##' in tokens:
                        raise PreprocessError('macro stringification/token pasting unsupported')
                    self.macros[name] = (params,tokens)
                elif command == 'undef':
                    self.macros.pop(value,None)
                elif command == 'include':
                    spec = ''.join(self.expand(TOKEN.findall(value)))
                    match = re.fullmatch(r'["<]([^">]+)[">]', spec)
                    if not match: raise PreprocessError('invalid #include')
                    dirs = ([Path(filename).parent] if spec.startswith('"') else []) + self.includes
                    path = next((d/match[1] for d in dirs if (d/match[1]).is_file()), None)
                    if path is None: raise PreprocessError(f'include not found: {match[1]}')
                    self.dependencies.append(str(path.resolve()))
                    output.append(self.process(path.read_text(encoding='utf-8-sig'), str(path), depth+1))
                elif command == 'error':
                    raise PreprocessError(value)
                elif command:
                    raise PreprocessError(f'unsupported directive #{command}')
            except PreprocessError as e:
                raise PreprocessError(f'{filename}:{number}: {e}') from None
        flush()
        if conditions: raise PreprocessError(f'{filename}: unterminated conditional')
        return '\n'.join(output)+'\n'
