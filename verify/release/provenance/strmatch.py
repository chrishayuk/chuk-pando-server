import sys, json
ours=[json.loads(l) for l in open(sys.argv[1])]; theirs=[json.loads(l) for l in open(sys.argv[2])]
minlen=int(sys.argv[3])
th={}
for t in theirs:
    if t['kind']=='string':
        s=t['text'].strip()
        if len(s)>=minlen: th.setdefault(s,[]).append(f"{t['file']}:{t['line']}")
seen=set()
for o in ours:
    if o['kind']!='string': continue
    s=o['text'].strip()
    if len(s)>=minlen and s in th and s not in seen:
        seen.add(s); print(f"{o['file']}:{o['line']}\t{s!r}\t{th[s][:2]}")
