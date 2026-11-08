#!/usr/bin/env python3
"""Exhaustive explicit-state model checker for the abstract protocol of MODEL.md.

Abstraction: objects are commits (id, parents); a pack is a set of commits plus the thin
delta bases its entries use. Server state = refs (CAS), ref versions (tip, pack, deps, ls),
packs (objects, introducing versions). Client state = object set + known tips.

Actions (all interleavings explored, BFS up to --depth actions):
  commit(c, r)      client c makes a commit on top of its tip for r (or a root commit)
  push(c, rs)       push one or two refs in ONE pack against the advertised tips the client has
                    (old-id = client's last known tip => stale old-ids give real CAS races);
                    each entry may use a thin base = newest commit reachable from the negatives
  delete(c, r)      CAS delete
  fetch(c, r)       plan (held ranges from tip-index haves, stop at held versions), send whole
                    packs, keep thin deltas only if every version introducing the base's pack is
                    held, else expand
  gc(c)             client keeps only objects reachable from its tips (git gc --prune=now)
Invariants:
  I1 every server ref's closure contains Reach(tip)
  I5 a fetch never needs a thin base the client lacks, and ends with Reach(want) present
  I2 a push is accepted iff old-id == current tip (per ref; atomic pushes all-or-nothing)
Usage: model_check.py [--depth N] [--clients C] [--refs R] [--commits K]
"""
import argparse
import sys
from collections import deque

ZERO = 0  # commit id 0 = "no commit"
BUG = None  # mutation testing of the checker: --bug deps-first-rv | holds-first-rv


def reach(commits, tips):
    seen, st = set(), [t for t in tips if t]
    while st:
        c = st.pop()
        if c in seen:
            continue
        seen.add(c)
        st.extend(commits[c])
    return seen


class S:
    """Immutable state; tuples for hashing."""
    __slots__ = ("commits", "refs", "rvs", "packs", "clients", "tipidx", "covers")

    def __init__(self, commits, refs, rvs, packs, clients, tipidx, covers=frozenset()):
        self.covers = covers        # frozenset of (r, lo, hi, pack, deps, tips)
        self.commits = commits      # tuple of parents-tuples, index = id (0 unused)
        self.refs = refs            # tuple per ref: (seq, tip)
        self.rvs = rvs              # frozenset of (r, s, tip, pack, deps, ls)
        self.packs = packs          # tuple of (objs frozenset, thin frozenset of (obj, base), rvs frozenset)
        self.clients = clients      # tuple of (objs frozenset, tips tuple per ref)
        self.tipidx = tipidx        # frozenset of (tip, r, s)  (first writer)

    def key(self):
        return (self.commits, self.refs, self.rvs, self.packs, self.clients, self.tipidx, self.covers)


def rv_map(st):
    return {(r, s): (tip, pack, deps, ls) for (r, s, tip, pack, deps, ls) in st.rvs}


def closure_packs(st, roots):
    m = rv_map(st)
    seen, out, stack = set(), set(), list(roots)
    while stack:
        v = stack.pop()
        if v in seen:
            continue
        seen.add(v)
        tip, pack, deps, ls = m[v]
        if pack is not None:
            out.add(pack)
        stack.extend(deps)
    return out


def first_pack(st, obj):
    for i, (objs, _thin, _rvs) in enumerate(st.packs):
        if obj in objs:
            return i
    return None


def do_push(st, c, rs, nrefs, violations, desc):
    commits = {i: p for i, p in enumerate(st.commits)}
    cobjs, ctips = st.clients[c]
    news = [ctips[r] for r in rs]
    if any(n == ZERO for n in news):
        return None
    adv = [tip for (_seq, tip) in st.refs if tip]
    negs = [t for t in adv if t in cobjs]
    excl = reach(commits, negs)
    pack_objs = frozenset(reach(commits, news) - excl)
    # thin base: newest commit reachable from the negatives (often another ref's tip)
    base = max(excl) if excl else None
    thin = frozenset((o, base) for o in pack_objs if base is not None)
    prereqs = set()
    for o in pack_objs:
        prereqs.update(p for p in commits[o] if p not in pack_objs)
    prereqs.update(n for n in news if n not in pack_objs)
    # dependencies: all versions that introduced the first committed pack containing each prereq
    deps = set()
    for p in prereqs:
        i = first_pack(st, p)
        if i is None:
            return None  # server rejects: missing necessary objects (client error in model)
        intro = st.packs[i][2]
        if BUG == "deps-first-rv":
            intro = {min(intro)}
        deps.update(intro)
    for (o, b) in thin:
        if first_pack(st, b) is None:
            return None
    # per-ref CAS, non-atomic
    refs = list(st.refs)
    rvs = set(st.rvs)
    tipidx = set(st.tipidx)
    tipset = {t for (t, _, _) in st.tipidx}
    introduced = set()
    accepted = []
    for r in rs:
        seq, cur = refs[r]
        old = st.clients[c][1][nrefs + r]  # last known server tip for r
        ok = cur == old
        if not ok:
            continue
        s = seq + 1
        # linked only if the old tip is a boundary parent reached from THIS ref's new tip
        # through commits of the pack (multi-ref packs: the boundary may belong to another ref)
        if BUG == "linked-pack-boundary":
            linked = cur != ZERO and cur in prereqs and len(pack_objs) > 0
        else:
            own = reach({k: v for k, v in commits.items()}, [ctips[r]]) & pack_objs
            own_boundary = {p for o in own for p in commits[o] if p not in pack_objs}
            linked = cur != ZERO and cur in own_boundary
        d = set(deps)
        prev_ls = next((ls for (rr, ss, _t, _p, _d, ls) in st.rvs if rr == r and ss == seq), s)
        if linked:
            d.add((r, seq))
        rvs.add((r, s, ctips[r], len(st.packs) if pack_objs else None, frozenset(d), prev_ls if linked else s))
        refs[r] = (s, ctips[r])
        if ctips[r] not in tipset:
            tipidx.add((ctips[r], r, s))
            tipset.add(ctips[r])
        introduced.add((r, s))
        accepted.append(r)
    packs = list(st.packs)
    if introduced and pack_objs:
        packs.append((pack_objs, thin, frozenset(introduced)))
    # client: remember pushed refs as known server tips
    cl = list(st.clients)
    tips = list(ctips)
    for r in accepted:
        tips[nrefs + r] = tips[r]
    cl[c] = (cobjs, tuple(tips))
    return S(st.commits, tuple(refs), frozenset(rvs), tuple(packs), tuple(cl), frozenset(tipidx), st.covers)


def do_delete(st, c, r, nrefs):
    seq, cur = st.refs[r]
    old = st.clients[c][1][nrefs + r]
    if cur == ZERO or cur != old:
        return None
    s = seq + 1
    refs = list(st.refs)
    refs[r] = (s, ZERO)
    rvs = set(st.rvs)
    rvs.add((r, s, ZERO, None, frozenset(), s))
    cl = list(st.clients)
    cobjs, ctips = cl[c]
    tips = list(ctips)
    tips[nrefs + r] = ZERO
    cl[c] = (cobjs, tuple(tips))
    return S(st.commits, tuple(refs), frozenset(rvs), st.packs, tuple(cl), st.tipidx, st.covers)


SPANS = (2, 4)  # model fanout 2: L1 spans 2 versions, L2 spans 4


def do_compact(st, r):
    """Build every missing cover of ref r (L1 from versions, L2 from two L1 children)."""
    m = rv_map(st)
    seq = st.refs[r][0]
    covers = {(cr, lo, hi): (p, d, t) for (cr, lo, hi, p, d, t) in st.covers}
    packs = list(st.packs)
    changed = False
    for li, span in enumerate(SPANS):
        for hi in range(span, seq + 1, span):
            lo = hi - span
            if (r, lo, hi) in covers:
                continue
            if li == 0:
                members = [m[(r, s)] for s in range(lo + 1, hi + 1)]
                mpacks = [p for (_t, p, _d, _ls) in members if p is not None]
                deps = set().union(*[d for (_t, _p, d, _ls) in members])
                tips = frozenset(t for (t, _p, _d, _ls) in members if t)
            else:
                cs = SPANS[li - 1]
                kids = [covers[(r, hi - (k + 1) * cs, hi - k * cs)] for k in range(span // cs)]
                mpacks = [p for (p, _d, _t) in kids if p is not None]
                deps = set().union(*[d for (_p, d, _t) in kids])
                tips = frozenset().union(*[t for (_p, _d, t) in kids])
            deps = frozenset(d for d in deps if not (d[0] == r and lo < d[1] <= hi))
            if BUG == "cover-drop-own-deps":
                deps = frozenset(d for d in deps if d[0] != r)
            objs = frozenset().union(*[packs[p][0] for p in mpacks]) if mpacks else frozenset()
            pidx = None
            if objs:
                packs.append((objs, frozenset(), frozenset()))
                pidx = len(packs) - 1
            covers[(r, lo, hi)] = (pidx, deps, tips)
            changed = True
    if not changed:
        return None
    cov = frozenset((cr, lo, hi, p, d, t) for (cr, lo, hi), (p, d, t) in covers.items())
    return S(st.commits, st.refs, st.rvs, tuple(packs), st.clients, st.tipidx, cov)


def do_fetch(st, c, r, nrefs, violations, desc):
    seq, want = st.refs[r]
    if want == ZERO:
        return None
    commits = {i: p for i, p in enumerate(st.commits)}
    cobjs, ctips = st.clients[c]
    m = rv_map(st)
    tipmap = {t: (rr, ss) for (t, rr, ss) in st.tipidx}
    held = {}
    for h in set(ctips):
        if h and h in cobjs and h in tipmap:
            rr, ss = tipmap[h]
            held.setdefault(rr, []).append((m[(rr, ss)][3], ss))

    def is_held(v):
        return any(lo <= v[1] <= hi for (lo, hi) in held.get(v[0], []))

    want_v = tipmap[want]
    haves = {t for t in ctips if t and t in cobjs}
    covers = {(cr, lo, hi): (p, d, t) for (cr, lo, hi, p, d, t) in st.covers}
    seen, sent, stack = set(), [], [want_v]
    while stack:
        v = stack.pop()
        if v in seen or is_held(v):
            continue
        seen.add(v)
        jumped = False
        for span in reversed(SPANS):
            if v[1] % span:
                continue
            lo = v[1] - span
            cov = covers.get((v[0], lo, v[1]))
            if cov is None:
                continue
            if any(lo < hh <= v[1] for (_l, hh) in held.get(v[0], [])):
                continue
            if cov[2] & haves:
                continue
            if cov[0] is not None and cov[0] not in sent:
                sent.append(cov[0])
            stack.extend(cov[1])
            jumped = True
            break
        if jumped:
            continue
        tip, pack, deps, ls = m[v]
        if tip in ctips and tip in cobjs:
            continue
        if pack is not None and pack not in sent:
            sent.append(pack)
        stack.extend(deps)
    sent_objs = set()
    for p in sent:
        sent_objs |= st.packs[p][0]
    new_objs = set(cobjs) | sent_objs
    for p in sent:
        for (o, b) in st.packs[p][1]:
            if b in sent_objs:
                continue
            bp = first_pack(st, b)
            rvs_b = st.packs[bp][2]
            if BUG == "holds-first-rv":
                rvs_b = {min(rvs_b)}
            if all(is_held(v) for v in rvs_b):
                if b not in cobjs:
                    violations.append(f"I5 thin base {b} kept thin but client lacks it: {desc}")
            # else: expanded by the server (no requirement on the client)
    missing = reach(commits, [want]) - new_objs
    if missing:
        violations.append(f"I5 fetch of ref {r} incomplete, missing {sorted(missing)}: {desc}")
    cl = list(st.clients)
    tips = list(ctips)
    tips[r] = want
    tips[nrefs + r] = want
    cl[c] = (frozenset(new_objs), tuple(tips))
    return S(st.commits, st.refs, st.rvs, st.packs, tuple(cl), st.tipidx, st.covers)


def check_closures(st, violations, desc):
    commits = {i: p for i, p in enumerate(st.commits)}
    m = rv_map(st)
    for r, (seq, tip) in enumerate(st.refs):
        if tip == ZERO:
            continue
        objs = set()
        for p in closure_packs(st, [(r, seq)]):
            objs |= st.packs[p][0]
        miss = reach(commits, [tip]) - objs
        if miss:
            violations.append(f"I1 ref {r}@{seq} closure misses {sorted(miss)}: {desc}")


def successors(st, a):
    nrefs, nclients, maxc = a.refs, a.clients, a.commits
    for c in range(nclients):
        cobjs, ctips = st.clients[c]
        # commit
        if len(st.commits) - 1 < maxc:
            for r in range(nrefs):
                parent = ctips[r]
                cid = len(st.commits)
                commits = st.commits + ((parent,) if parent else (),)
                tips = list(ctips)
                tips[r] = cid
                cl = list(st.clients)
                cl[c] = (cobjs | {cid}, tuple(tips))
                yield f"commit(c{c},r{r})", S(commits, st.refs, st.rvs, st.packs, tuple(cl), st.tipidx, st.covers)
                # unrelated history (force-push material): a root commit for r
                if ctips[r]:
                    commits3 = st.commits + ((),)
                    tips3 = list(ctips)
                    tips3[r] = cid
                    cl3 = list(st.clients)
                    cl3[c] = (cobjs | {cid}, tuple(tips3))
                    yield f"commit_root(c{c},r{r})", S(commits3, st.refs, st.rvs, st.packs, tuple(cl3), st.tipidx, st.covers)
                # cross-ref commit: new commit for r on top of another ref's tip
                for src in range(nrefs):
                    if src != r and ctips[src]:
                        commits2 = st.commits + ((ctips[src],),)
                        tips2 = list(ctips)
                        tips2[r] = cid
                        cl2 = list(st.clients)
                        cl2[c] = (cobjs | {cid}, tuple(tips2))
                        yield f"commit_from(c{c},r{r}<-r{src})", S(commits2, st.refs, st.rvs, st.packs, tuple(cl2), st.tipidx, st.covers)
        for r in range(nrefs):
            yield f"push(c{c},r{r})", ("push", c, (r,))
            yield f"fetch(c{c},r{r})", ("fetch", c, r)
            if a.deletes:
                yield f"delete(c{c},r{r})", ("delete", c, r)
            if a.covers and c == 0:
                yield f"compact(r{r})", ("compact", c, r)
        if nrefs >= 2:
            yield f"push2(c{c})", ("push", c, (0, 1))
        yield f"gc(c{c})", ("gc", c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=7)
    ap.add_argument("--clients", type=int, default=2)
    ap.add_argument("--refs", type=int, default=2)
    ap.add_argument("--commits", type=int, default=4)
    ap.add_argument("--bug", default=None)
    ap.add_argument("--covers", action="store_true", help="add compaction actions (fanout 2)")
    ap.add_argument("--deletes", action="store_true", help="add CAS delete actions")
    ap.add_argument("--spans", default="2,4", help="cover spans per level (aligned, each a multiple of the previous)")
    a = ap.parse_args()
    global BUG, SPANS
    BUG = a.bug
    SPANS = tuple(int(x) for x in a.spans.split(","))
    n = a.refs
    init = S((None,), tuple((0, ZERO) for _ in range(n)), frozenset(), tuple(),
             tuple((frozenset(), tuple([ZERO] * (2 * n))) for _ in range(a.clients)), frozenset())
    seen = {init.key()}
    q = deque([(init, 0, "")])
    violations = []
    states = 0
    while q:
        st, d, path = q.popleft()
        states += 1
        if d >= a.depth:
            continue
        for name, nxt in successors(st, a):
            desc = (path + " " + name).strip()
            if isinstance(nxt, tuple):
                kind = nxt[0]
                if kind == "push":
                    nxt = do_push(st, nxt[1], nxt[2], n, violations, desc)
                elif kind == "fetch":
                    nxt = do_fetch(st, nxt[1], nxt[2], n, violations, desc)
                elif kind == "delete":
                    nxt = do_delete(st, nxt[1], nxt[2], n)
                elif kind == "compact":
                    nxt = do_compact(st, nxt[2])
                else:
                    c = nxt[1]
                    cobjs, ctips = st.clients[c]
                    commits = {i: p for i, p in enumerate(st.commits)}
                    keep = frozenset(reach(commits, [t for t in ctips if t in cobjs]) & cobjs)
                    cl = list(st.clients)
                    cl[c] = (keep, ctips)
                    nxt = S(st.commits, st.refs, st.rvs, st.packs, tuple(cl), st.tipidx, st.covers)
                if nxt is None:
                    continue
                check_closures(nxt, violations, desc)
            k = nxt.key()
            if k in seen:
                continue
            seen.add(k)
            q.append((nxt, d + 1, desc))
        if len(violations) > 20:
            break
    print(f'{{"states": {states}, "distinct": {len(seen)}, "depth": {a.depth}, "violations": {len(violations)}}}')
    for v in violations[:20]:
        print("  " + v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
