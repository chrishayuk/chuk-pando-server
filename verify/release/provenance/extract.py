"""Extract comments and string literals from C/Rust (C-like) and Python/shell (#) files.
Usage: python3 -I extract.py <mode:c|hash> <file>...  -> JSON lines {file,line,kind,text}
"""
import sys, json, re

def c_like(src):
    out = []  # (line, kind, text)
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == '\n':
            line += 1; i += 1; continue
        if src.startswith('//', i):
            j = src.find('\n', i)
            if j < 0: j = n
            out.append((line, 'comment', src[i+2:j])); i = j; continue
        if src.startswith('/*', i):
            j = src.find('*/', i+2)
            if j < 0: j = n
            txt = src[i+2:j]
            out.append((line, 'comment', txt)); line += txt.count('\n'); i = j+2; continue
        # rust raw strings r"..." / r#"..."#
        m = re.match(r'b?r(#*)"', src[i:i+10])
        if m and (i == 0 or not (src[i-1].isalnum() or src[i-1] == '_')):
            hashes = m.group(1)
            start = i + m.end()
            end = src.find('"' + hashes, start)
            if end < 0: end = n
            txt = src[start:end]
            out.append((line, 'string', txt)); line += txt.count('\n'); i = end + 1 + len(hashes); continue
        if c == '"':
            j = i+1
            while j < n and src[j] != '"':
                if src[j] == '\\': j += 1
                j += 1
            txt = src[i+1:j]
            out.append((line, 'string', txt)); line += txt.count('\n'); i = j+1; continue
        if c == "'":
            # char literal or rust lifetime; skip short
            m = re.match(r"'(\\.[^']*|[^'\\])'", src[i:i+12])
            if m: i += m.end(); continue
            i += 1; continue
        i += 1
    return out

def hash_like(src):
    out = []
    for ln, l in enumerate(src.split('\n'), 1):
        s = l.strip()
        if s.startswith('#') and not s.startswith('#!'):
            out.append((ln, 'comment', s[1:]))
        else:
            k = l.find(' #')
            if k >= 0:
                out.append((ln, 'comment', l[k+2:]))
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', l):
            out.append((ln, 'string', m.group(1) or m.group(2) or ''))
    return out

mode = sys.argv[1]
for f in sys.argv[2:]:
    try:
        src = open(f, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    items = c_like(src) if mode == 'c' else hash_like(src)
    for ln, k, t in items:
        print(json.dumps({'file': f, 'line': ln, 'kind': k, 'text': t}))
