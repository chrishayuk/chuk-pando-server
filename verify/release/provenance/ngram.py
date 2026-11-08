"""Shared word n-gram finder.
python3 -I ngram.py <ours.jsonl> <theirs.jsonl> <n> <kind> [docs_corpus.txt]
Reports each of our items that shares an n-word sequence with any of their items of the same kind.
If docs corpus given, marks whether the n-gram also appears in Git's protocol docs.
"""
import sys, json, re
from collections import defaultdict

def words(t):
    t = t.lower()
    return re.findall(r"[a-z0-9_]+", t)

ours = [json.loads(l) for l in open(sys.argv[1])]
theirs = [json.loads(l) for l in open(sys.argv[2])]
N = int(sys.argv[3]); kind = sys.argv[4]
docs = None
if len(sys.argv) > 5:
    dw = words(open(sys.argv[5], errors='replace').read())
    docs = set(tuple(dw[i:i+N]) for i in range(len(dw)-N+1))

index = defaultdict(list)
for it in theirs:
    if it['kind'] != kind: continue
    w = words(it['text'])
    for i in range(len(w)-N+1):
        g = tuple(w[i:i+N])
        if len(index[g]) < 5:
            index[g].append((it['file'], it['line']))

# merge consecutive comment lines of ours (// comments split per line) by file
merged = []
cur = None
for it in ours:
    if it['kind'] != kind: continue
    if cur and cur['file'] == it['file'] and it['line'] == cur['end'] + 1 and kind == 'comment':
        cur['text'] += ' ' + it['text']; cur['end'] = it['line']
    else:
        if cur: merged.append(cur)
        cur = dict(it); cur['end'] = it['line']
if cur: merged.append(cur)

hits = 0
for it in merged:
    w = words(it['text'])
    i = 0
    while i <= len(w)-N:
        g = tuple(w[i:i+N])
        if g in index:
            # extend the run
            j = i + N
            while j < len(w) and tuple(w[j-N+1:j+1]) in index:
                j += 1
            run = ' '.join(w[i:j])
            indoc = (docs is not None and g in docs)
            print(f"{it['file']}:{it['line']}  [{j-i} words]{' [ALSO-IN-DOCS]' if indoc else ''}\n   ours : {run}\n   their: {index[g][:3]}")
            hits += 1
            i = j
        else:
            i += 1
print(f'# total runs: {hits}', file=sys.stderr)
