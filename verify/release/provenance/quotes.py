import sys, re
art = open(sys.argv[1]).read()
artq = [q for q in re.findall(r'"([^"]+)"', art)]
norm = lambda s: ' '.join(re.findall(r"[a-z0-9]+", s.lower()))
artq_n = [norm(q) for q in artq]
for f in sys.argv[2:]:
    for ln, l in enumerate(open(f, errors='replace'), 1):
        cands = []
        for m in re.finditer(r'"([^"\n]{3,})"|“([^”\n]{3,})”', l):
            cands.append(('q', m.group(1) or m.group(2)))
        if l.lstrip().startswith('>'):
            cands.append(('bq', l.lstrip()[1:].strip()))
        for kind, q in cands:
            w = len(q.split())
            if w < 3: continue
            n = norm(q)
            inart = any(n and (n in a or a in n) and len(a) > 8 for a in artq_n)
            ctx = bool(re.search(r'article|openai|OpenAI', l))
            if inart or ctx or kind == 'bq':
                print(f"{f}:{ln}\t{kind}\t{w}w\t{'ARTICLE-QUOTE' if inart else ('ctx' if ctx else '')}\t{q[:150]}")
