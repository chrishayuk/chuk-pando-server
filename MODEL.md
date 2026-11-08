# MODEL — ref versions, dependencies, push/fetch and compaction protocol

This is the precise model the server implements (code references in brackets). It is written
so that it can be transcribed into TLA+/P/Quint; §5 lists the invariants, §6 the trace
vocabulary emitted in `G_DATA_DIR/events/events.jsonl`, and §7 the deterministic simulator.

## 1. State

Per repository `R` (all keys below are prefixed by `R`; `\x01` separates key parts) [repo.rs].

| Variable | Store key | Mutability |
|---|---|---|
| `Blob[p]` pack bytes, `Idx[p]` sidecar | `blobs/repos/<R>/packs/<p>.pack/.pidx` | immutable, write-once (id = SHA-1 trailer) |
| `Ref[r] = (seq, tip, ls, peeled)` | `ref/R/r` | mutable, compare-and-swap only |
| `RV[r,s] = (tip, pack?, deps, linked, ls, peeled)` | `rv/R/r/s` | immutable, write-once |
| `Tip[o] = (r,s)` | `tip/R/o` | write-once (first writer wins) |
| `Pack[p] = (level, rvs, cover?)` | `pack/R/p` | created once; `rvs` append-only (every ref version introduced with `p`) |
| `Cover[r,L,hi] = (pack, lo, hi, deps, tips, linked)` | `cover/R/r/L/hi` | write-once |
| `Twin[p] = p'` (self-contained copy of thin L0 pack `p`, `Objects(p') = Objects(p)`) | `twin/R/p` | write-once |

`Objects(p)` = object ids contained in pack `p`; `Ext(p)` = graph prerequisites of `p`
= ids referenced by objects of `p` (commit tree/parents, tree entries except gitlinks, tag
target) or named as a new ref tip, and not contained in `p`. `Bases(p)` = REF_DELTA bases
not contained in `p` (thin-pack bases); each is recorded with the pack it was resolved from.
Both are computed by the single-pass decode and stored in `Idx[p]` [ingest.rs].

`deps` are ref-version ids `(r', s')`. Closure of a ref version:

    Closure(v)  = {v} ∪ ⋃_{d ∈ deps(v)} Closure(d)
    Packs(v)    = { pack(u) | u ∈ Closure(v), pack(u) ≠ ⊥ }
    Objs(v)     = ⋃_{p ∈ Packs(v)} Objects(p)

Linking: `linked(r,s)` ⇔ `(r,s-1) ∈ deps(r,s)` and `tip(r,s-1)` is a boundary (parent)
commit of `pack(r,s)` (so it is an ancestor of `tip(r,s)`). `ls(r,s) = ls(r,s-1)` if linked,
else `s`. Consequence: `(r,j) ∈ Closure(r,s)` for every `ls(r,s) ≤ j ≤ s`.

## 2. Metadata store semantics [meta.rs]

Multi-key transactions with optimistic concurrency: a transaction records `(key, version)`
for every key it reads and buffers writes; `commit` locks exactly the touched keys (written keys
exclusive, keys only read shared, all at once; until 2026-11-07 hashed into 4096 stripes, see
docs/AUDIT-NET-REMEDIATION.md F12), validates that every read key still has the recorded version, appends
one WAL record (all writes, one LSN), waits for the group-commit fsync, applies, unlocks.
Validation failure ⇒ `Conflict`, nothing written. Transactions touching disjoint keys never
conflict and never wait for each other's validation. A WAL write/fsync error aborts the
process (state is then decided by recovery). Recovery replays the snapshot and the WAL
prefix of complete, CRC-valid records; a torn tail is truncated. Hence: a transaction is
durable iff its WAL record is complete; every acknowledged transaction is durable;
transactions are applied in LSN order. Per-key serializability follows from version
validation on the ref key.

## 3. Push protocol [proto.rs handle_receive, repo.rs Core::push]

Input: command list `[(old_i, new_i, r_i)]`, optional pack bytes, flags `atomic`,
`explicit-haves`.

1. **Ingest** (no metadata access): stream the pack to `blobs/tmp/…`, single-pass decode:
   every entry inflated, deltas resolved (in-pack bases; thin bases looked up in committed
   packs), object ids computed, objects parsed and structurally validated, trailer checksum
   verified. Any failure ⇒ `unpack <error>`, all commands `ng`, nothing stored.
2. **Store blob**: fsync file, write `.pidx`, fsync, rename both into place (read-only), fsync
   directory. *Blob durable before any metadata references it.*
3. **Dependencies** (no metadata reads; uses the server's index of committed L0 packs):
   `X = Ext(p) ∪ {new_i not in p}`. For each `x ∈ X` find a committed L0 pack containing it and
   the ref versions `Pack[q].rvs` that introduced that pack (all of them when one pack introduced
   several refs); if none ⇒ reject all commands
   (`missing necessary objects`). `D = {u}` reduced by same-ref subsumption
   (`(r,j)` dropped if some `(r,k) ∈ D` has `ls(r,k) ≤ j ≤ k`). `B` = commits in `X`.
4. **Commit** — one transaction per command (or one for all commands if `atomic`):
   - `cur = Ref[r]` (read, version recorded). If `cur.tip ≠ old` ⇒ `ng` (`cannot lock ref …`).
   - `s = cur.seq + 1` (seq continues across delete/recreate).
   - delete: `RV[r,s] = (0, ⊥, [], false, s)`; `Ref[r] = (s, 0)`.
   - create/update: `linked = cur.tip ≠ 0 ∧ p non-empty ∧ cur.tip is a parent (outside p) of a
     commit of p reachable from new through commits of p` (per ref — in a multi-ref pack a
     boundary commit may belong to another ref's history; the earlier rule `cur.tip ∈ B` was
     unsound and was found by `tests/model_check.py`);
     `deps = D ∪ ({(r,cur.seq)} if linked)`; `RV[r,s] = (new, p?, deps, linked, ls)`;
     `Ref[r] = (s, new, ls)`; `Tip[new] = (r,s)` if absent; `Pack[p]` if absent.
   - commit; on `Conflict` re-run step 4 (re-reading `Ref[r]`).
5. **Report** `ok r` only after the commit returned (durable). Metadata cost per command:
   reads `Ref[r]`, `Tip[new]`, `Pack[p]`; writes ≤ 4 keys — independent of pack size.

Advertisement [proto.rs receive_advertisement]: stock clients get every ref (old-ids for CAS).
Clients that send `Git-Protocol: explicit-haves` additionally get the `explicit-haves`
capability and a single `<trunk tip> .have`, so their pack prerequisites are trunk-only while
named refs still carry the old-ids used for update checks / `--force-with-lease`.

## 4. Fetch protocol [repo.rs plan_fetch, packout.rs, proto.rs fetch]

### 4.1 Held set
For each `have h` (first 256): if `Tip[h] = (r,k)` exists, the client *holds* `[ls(r,k), k]`
of ref `r` and `h` is ACKed. `held(r,j)` ⇔ `j` lies in a held range of `r`.
Soundness of stopping at held versions: the client holds `Reach(tip v)`. Every dependency edge
`w → u` exists because some object reachable from `tip(w)` lies in a pack introduced by `u`; if
that pack introduced several refs, `w` depends on all of them. So whenever an object needed by a
want is in a pack of a held version's closure, either it is reachable from a held tip (the
client has it) or some non-held version also introduced that pack and the walk emits it.
Residual assumption A1: a single-ref pack contains only objects reachable from its ref's tip
(true for packs produced by `git pack-objects --revs`). Partially failed non-atomic multi-ref
pushes leave objects of the failed refs in the pack; they are unreachable from every advertised
tip, so no client can omit them from a later pack (pushers only omit objects reachable from
advertised tips / `.have`), hence any later version reaching them has them in its own pack.
Thin-delta bases are only kept thin when *every* version that introduced the base's pack is held
(`client_holds`), otherwise expanded.

### 4.2 Plan
Walk from the want versions (`Tip[want]`), DFS, visited-set:

    visit(r,s):  if held(r,s): stop
                 for L = 4..1 with s mod 4^L = 0:
                     if Cover[r,L,s] exists and no held version in (s-4^L, s]
                        and no cover tip is a have:
                         emit cover pack; visit each d ∈ cover.deps; return
                 v = RV[r,s]; if tip(v) is a have: stop
                 emit pack(v); visit each d ∈ deps(v)

Packs are sent in reverse emission order (dependencies first). On a linear history the walk
from `(r,b)` to a held `(r,a)` touches O(4·log₄(b−a)) records (aligned covers act as a skip
list); without covers it touches b−a records.

### 4.3 Covers (compaction) [compact.rs]
For every ref and level `L ∈ 1..4`, for every aligned `hi` (`hi mod 4^L = 0`, `hi ≤ Ref[r].seq`):
`Cover[r,L,hi]` with `lo = hi − 4^L`:
- members: `RV[r,lo+1..hi]` at every level (their L0 packs are spliced; building from the four
  child covers instead would carry every child's full chain-head copies upward — measured 5.9x
  at L1 and no gain above, see LOG 10:30);
- pack = splice(member packs in version order) with duplicates removed, OFS deltas kept when
  the base is inside, everything else expanded ⇒ no REF_DELTA, no external base;
- `deps = ⋃ member deps \ {(r,j) | lo < j ≤ hi}`; `tips` = member tips; `linked` = all linked.
Correctness: `Objs(cover) ∪ Objs(deps) ⊇ Objs(r,hi)` (each member's pack is inside, each
member's deps are either members or in `deps`). Covers are written blob-first, then one
transaction (`Cover`, `Pack`), idempotent. Covers never replace L0 packs; nothing is deleted.
Built in the background after each push reaching an aligned version, and by `maintain`.

### 4.4 Pack emission
Stock clients: one inline pack spliced from the planned packs (compressed data copied
verbatim). Per entry: duplicates (already emitted) dropped; a delta whose base was emitted
becomes OFS (or REF without `ofs-delta`); a REF delta whose base is not in the output is kept
thin only if the client sent `thin-pack` and holds the base (§4.1), else expanded. A whole
object taken from a cover is replaced by its L0 entry when that entry is a delta whose base was
already emitted (or is held, under the same thin-pack rule) — same object, smaller encoding.
`packfile-uris` clients: every planned pack that is self-contained (non-thin, and without
REF_DELTA if `no-ref-delta`) — or, for a thin L0 pack, its *thick twin* `Twin[p]` (a
write-once self-contained copy of exactly `Objects(p)`, external delta bases expanded, built on
first demand by a packfile-URI fetch and shared by all later ones) — is sent as a URI `http://<cache node>/packs/<R>/<p>.pack`
(owner = consistent hash of `R/p`), with its id = SHA-1 trailer as the hash; the inline pack
carries the rest, with any delta whose base lives in a URI pack expanded (the inline pack is
indexed first by the client).

## 5. Invariants (checked by `pando sim`, and by the test scripts)

- **I1 Closure**: for every ref `r` with `Ref[r].tip = t ≠ 0`: `Reach(t) ⊆ Objs(plan(t, ∅))`.
  Follows inductively from step 3 (every `Ext(p)` object is in a committed pack whose
  introducing version is a dependency) and from blob-before-metadata.
- **I2 CAS / exactly one winner**: a command `(old,new,r)` is acknowledged only if
  `Ref[r].tip = old` at its commit LSN. Two commands with the same `old` on `r`: at most one ok.
- **I3 Durability / RYOW**: an acknowledged update is visible to every later read, including
  after any number of crashes (WAL fsync before ack; reads go to the store).
- **I4 Monotonic ref**: `Ref[r].seq` strictly increases; a reader never observes a smaller seq
  for `r` after a larger one.
- **I5 Fetch completeness**: the client can index every returned pack (thin bases only when
  held) and ends with `Reach(want)` connected.
- **I6 Immutability**: a pack/sidecar/RV/Cover/Tip/Pack record never changes after creation.
- **I7 Cover soundness**: `Objs(Cover[r,L,hi]) = ⋃ Objects(members)`; L0 packs retained.
- **Permitted**: no cross-ref snapshot — ls-refs reads each ref independently (§ consistency).

## 6. Trace vocabulary (JSON lines; `seq` is per-process strictly increasing)

`server_start{pid,port}`, `repo_created{repo}`, `receive_advertised{repo,refs,explicit_haves}`,
`push_received{push,cmds[{ref,old,new}],atomic,explicit_haves}`,
`pack_ingested{push,pack,objects,bytes,prereqs,thin_bases}`, `pack_stored{push,pack}`,
`pack_rejected{push,error}`, `push_rejected{push,reason,missing}`,
`ref_cas_failed{push,ref,msg}`,
`ref_version_committed{push,lsn,ref,version,tip,old,pack,deps,linked,ls}` (`version` = ref-version number `s`; `seq` is the trace counter),
`push_reported{push,unpack,results[{ref,ok,msg}]}`, `push_failed{push,error}`,
`ls_refs{repo,snapshot[[ref,seq,tip]]}`, `fetch_negotiate{wants,haves,acks}`,
`fetch_served{wants,haves,acks,packs,uri_packs,inline_objects,inline_bytes,expanded,plan_steps,covers_used,plan_metadata_reads}`,
`cover_committed{ref,level,lo,hi,pack,members,deps,linked,objects,bytes,lsn}`,
`compaction_error`, `request_error`, simulator-only: `sim_start`, `sim_step`, `sim_crash`,
`sim_restart`, `sim_fault_armed{point}`, `sim_unacked_applied`, `sim_violation{what}`, `sim_end`.

Trace checking recipe (implemented in `tests/trace_check.py events.jsonl`): replay
`ref_version_committed` in `lsn` order per `(repo,ref)` — `version` must be consecutive and
`old` must equal the previous `tip` (I2/I4); each `ok` in `push_reported` must have a matching
`ref_version_committed` (I3); every `deps` entry must refer to an earlier committed
`(ref,version)`; covers aligned with known deps; `ls_refs` snapshots (`[ref, version, tip]`)
taken by one server process must be monotone per ref (I4). A SIGKILL can tear the last line of
the log; the checker skips unparsable lines. Events are flushed per line but not fsynced, so
after a crash the log may lack the last events of acknowledged pushes (the metadata store is
the source of truth; the trace is a best-effort observation).

## 7. Deterministic simulation

`server/target/release/pando sim --seed N [--steps S --clients C --refs R --crash P --fault P --dir D]`
runs a single-threaded seeded scheduler (splitmix64) over synthetic clients that build real
Git objects and thin packs, and interleaves: receive-pack advertisement, push (stale
advertisements produce real CAS races; 30% follow an explicit-haves advertisement and then
use only the `.have` (trunk) tip as pack prerequisite; 25% carry two refs in one pack, 30% atomic; commits
sometimes reuse blobs from other branches), ls-refs + fetch (the client indexes the returned
pack and checks connectivity; 40% request packfile-uris — the client indexes the inline pack
first, then every URI pack without thickening and checks its hash; half of those also request
no-ref-delta and require every pack to be standalone), client `gc --prune=now` (drops objects
unreachable from its refs), compaction + thick twins, WAL checkpoints, metadata-commit faults (`before_wal`: commit fails, nothing
durable; `after_wal`: crash between fsync and apply) and crash/restart (all memory dropped,
optionally a torn WAL tail). Invariants I1–I7 are checked continuously; output is a JSON report
(exit 1 on any violation) and the trace in `D/events/events.jsonl` with logical timestamps —
the same seed yields a byte-identical trace.

## 8. Exhaustive model checking of the abstract protocol

`tests/model_check.py` is an explicit-state BFS checker of the abstract protocol (commits,
packs with exact prerequisites and thin bases, ref versions with dependencies and `ls`, CAS,
multi-ref pushes, root/cross-ref commits (force pushes), held-range fetch planning with
thin-base expansion, client `gc --prune=now`). Default bounds (2 clients, 2 refs, ≤4 commits,
depth 8) explore 1.29 M states; invariants I1 (closure), I5 (fetch completeness, thin bases
held). Mutation switches show it detects the three bugs found during development:
`--bug deps-first-rv` (multi-ref pack dependency on one version only),
`--bug holds-first-rv` (thin base kept when only one introducing version is held),
`--bug linked-pack-boundary` (linked decided by pack boundary instead of per ref).

Extended model (`--covers --deletes --spans 1,2`): compaction covers with the same jump rule as
the planner (§4.2) and CAS deletes. Depth 8: 5.05 M states, 0 violations. Mutant
`--bug cover-drop-own-deps` (cover deps lose the ref's own earlier versions) is detected.
`tests/run_all.sh` runs depth 7 and checks that all four mutants are detected.
