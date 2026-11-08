# DECISIONS

Each entry: assumption / decision, alternatives considered, why.

## D1 Metadata store: embedded Rust OCC KV store with a write-ahead log
- No PostgreSQL binary is available in the sandbox and the brief allows "PostgreSQL (or equivalent)".
- Implemented `meta.rs`: an in-process key/value store with multi-key optimistic transactions
  (per-key version check at commit = compare-and-swap), striped per-key commit locks (keys hashed
  to 4096 stripes; a transaction locks only the stripes of the keys it reads-for-validation or
  writes, in sorted order), and a single append-only WAL with group commit (one fsync covers all
  transactions that arrived while the previous fsync ran).
  *Amended 2026-11-07:* the stripes were replaced by exact per-key locks (written keys exclusive, keys only read
  shared, all acquired at once). Under striping, unrelated transactions waited for each other whenever their keys
  shared a stripe, which an independent re-audit observed as a push to another branch waiting behind a stalled
  commit (F12). See docs/AUDIT-NET-REMEDIATION.md.
- Pushes to different refs touch disjoint keys, so they never abort or wait for each other on a
  logical lock; they share only the WAL group-commit pipeline (which batches them).
- Alternatives: redb/sled (single-writer lock: every commit serialises), SQLite (C dependency,
  single writer), shelling to Postgres (not available). Sharding the store into several units with
  2PC was considered; not done (one process per G_DATA_DIR in the harness). The key layout is
  per-ref so it could be range-sharded by `(repo, ref)`.

## D2 Push advertisement and the stock-client compatibility problem
- Clients that do NOT send the `explicit-haves` extra parameter (stock Git): the server advertises
  ALL refs exactly like git-receive-pack. The client then sends the true old-id for every existing
  ref, so update / force-update / delete / --force-with-lease are compare-and-swap on old-id for
  every ref, and "exactly one winner" holds on every ref.
  Cost: the stock client may use any advertised ref tip as a pack prerequisite, so a stored L0 pack
  can depend on topic branches, not only trunk; the dependency set recorded is computed exactly
  from the pack contents (see D4), so correctness is unaffected, but packs are less reusable and
  advertisement is O(#refs).
- Clients that send the `explicit-haves` extra parameter (the modified client in
  reference/openai-git): the server advertises the `explicit-haves` capability (plus
  `no-ref-delta`, see D10), every named ref (used only for old-id / update checks /
  --force-with-lease) and a single `.have` line for the trunk tip. The client therefore builds its
  pack against trunk alone. Whether or not the client then requests `explicit-haves`, the server
  computes dependencies from the pack contents (D4), so a client that ignores the capability is
  still handled correctly (its pack merely depends on more refs).
- Cost of advertising all named refs: O(#refs) advertisement bytes per push (≈55 KB at 1,000
  branches) and one metadata range read; benefit: no client ever sends a "create" for an existing
  ref, so rejection semantics are git's own on every ref.
- Measured (tests/cg5.sh): explicit-haves topic pushes depend on trunk versions only, and a
  second push to the topic re-sends the earlier topic commits (as the article describes).

## D3 Pack ids and blob layout
- Pack id = hex SHA-1 trailer of the pack (what `git index-pack` prints). Identical pushes dedupe to
  the same immutable blob. Blobs are written to a temp file, fsynced, renamed, and the directory
  fsynced BEFORE the metadata transaction that references them commits.

## D4 Exact pack prerequisites
- During the single-pass decode the server records every object id the pack *references* but does
  not *contain*: REF_DELTA bases outside the pack (thin bases), commit parents/trees, tree entries
  (except gitlinks), tag targets, and ref new-ids not in the pack. That set is the pack's exact
  object prerequisite set and is stored in the pack sidecar.
- Ref-version dependencies are then chosen so that every prerequisite is contained in the object
  closure of the chosen ref versions: for each prerequisite, every ref version that introduced a
  committed L0 pack containing it (server-local object index, no metadata reads; *all* of them
  when one pack introduced several refs), reduced by same-ref subsumption; plus the updated ref's
  previous version when its tip is a parent of a commit reachable from *this ref's* new tip
  within the pack ("linked"; deciding this from the pack's boundary set alone was unsound for
  multi-ref pushes — found by tests/model_check.py, see LOG 09:20). If any prerequisite is not in any committed pack the push is rejected
  (`missing necessary objects`), never accepted with dangling dependencies. (An earlier plan used
  tip-index lookups of boundary commits and "current version of trunk/other refs" candidates; it
  was replaced because it could pick a version the pushing client never had — see D5.)
- The prerequisite list is in `<pack>.pidx`; the chosen dependencies are in the `rv/` record and
  in the `ref_version_committed` event.

## D5 Dependencies are chosen from graph prerequisites, thin bases are decode-time only
- A ref version depends on the ref versions that *introduced* the packs containing its graph
  prerequisites. Thin-pack delta bases are NOT dependencies: they are recorded per pack (with
  the pack they resolve from, never deleted) and handled when serving: a REF_DELTA whose base is
  not in the response is kept thin only if the client sent `thin-pack` and provably holds the
  base (§4.1 MODEL), otherwise expanded to a full object.
- Why: every dependency edge then exists because an object reachable from the dependent's tip
  lies in a pack introduced by the dependency (and multi-ref packs make the dependent depend on
  all their refs), which makes stopping at held versions sound (MODEL §4.1), so the fetch
  planner never walks the history a client already has. A thin base is kept thin only if every
  version that introduced the base's pack is held. Alternative (deps = all advertised refs) would make
  every pack depend on every ref and destroy reuse.
- Same-ref subsumption via `ls` (linked-since) keeps dependency lists short. Cross-ref
  subsumption is not attempted (a topic may list both `main@k` and `topic@j` where `topic@j`
  already covers `main@k`): cost is a slightly longer list, never wrong.

## D6 Server-local derived indexes; immutable-record cache
- Each server keeps (a) an index oid → committed L0 pack, built from `pack/` records + sidecars
  (refreshed from metadata on a miss), and (b) a cache of ref-version and cover records. Both are
  derived from immutable data, so they are never stale; they are not authoritative. Ref pointers
  are always read from the metadata store (read-your-own-write, monotonic).
- `_stats.metadata_reads` counts store read operations (point reads and range scans), not cache
  hits (reported as `metadata_cache_hits`).

## D7 Fetch negotiation
- A `have` is recognised only if it is the tip of some ref version (one point read per have, first
  256). The server ACKs recognised haves and says `ready` as soon as one is recognised; otherwise
  NAK and the client continues. Intermediate commits are not recognised (cost: possibly more data
  sent, never less). `include-tag` is ignored (git backfills tags).
- A client that sends `shallow` lines has truncated history: its haves only prove the have's own
  version (no linked range), and no delta is kept thin for it (found as a real failure:
  "unresolved deltas" on fetch into a depth-1 clone, LOG 08:15).

## D8 Inline pack = splice of stored packs
- Stock clients get one inline pack built by copying compressed entry data from the planned
  stored packs (deps first), dropping duplicate objects across packs and re-encoding only entry
  headers/delta base references. No delta search, no object graph walk, no per-client pack
  computation. Cost: objects present in a planned pack but already held by the client may be sent
  again (amplification visible in `fetch_served.inline_objects`).
- Covers are self-contained, so they hold the head of every delta chain as a whole object. When a
  cover is spliced into an inline pack, each such whole object is replaced by its original L0
  entry if that entry is a delta whose base is already in the output (or, for thin-pack clients,
  provably held). Measured (cg6): inline bytes ~250 B per version at every distance (1 KB at
  distance 4 instead of ~6 KB with the cover's whole copy), with log-sized planning unchanged.

## D9 Compaction geometry
- Fanout 4, levels L1..L4 (4, 16, 64, 256 ref versions), per ref, aligned ranges. Covers are built
  by splicing (dedup + keep in-cover OFS deltas, expand others), not by a fresh delta search, so
  they improve reuse/self-containment and remove duplicates but do not improve delta compression
  across L0 boundaries. Covers per ref (not repository-wide) because ref versions are only ordered
  per ref (no global sequence → no cross-ref contention).
- All packs are kept forever (brief). Orphan blobs from crashed pushes are not garbage-collected.

## D10 Packfile URIs and the cache service
- Three cache nodes run as separate listeners (ephemeral 127.0.0.1 ports) inside the server
  process, each with its own LRU; a pack's owner is chosen by consistent hashing of `repo/packid`
  (64 virtual nodes per node). URIs are only given for self-contained packs (git verifies the pack
  hash, so thin packs cannot be thickened client-side) and, for `no-ref-delta` clients, packs
  without REF_DELTA. Explicit-haves clients are also told `no-ref-delta` on push so their L0 packs
  are URI-servable immediately.

## D11 Process model / crash semantics
- `run.sh start` spawns `pando serve` in a new session (setsid); that process binds the socket,
  then writes `server.pid` (its own PID = process-group leader). Everything (HTTP, cache nodes,
  compactor) runs in that one process, so `kill -9 -PID` stops everything.
- A WAL write/fsync error aborts the process (fail-stop) rather than returning an error.
- The data directory is protected by `flock(meta/LOCK)`.

## D12 Shallow fetch is the one graph-walking path
- `deepen N` (protocol v2) cannot be answered with stored packs (a depth-limited history is not a
  union of ref-version packs), so the server walks commits from the wants to depth N (ignoring
  haves, like upload-pack's deepen walk), collects their trees/blobs, and writes a fresh pack
  (reusing raw entry bytes for non-delta entries). shallow-info carries `shallow`/`unshallow`.
  Cost: CPU proportional to the depth-N snapshot, no reuse. Relative deepen / since / exclude are
  rejected with an explicit error. Non-deepen fetches from shallow clients use the normal planner.

## D13 Thick twins for thin L0 packs; no per-commit metadata
- Stock pushes are thin (REF_DELTA to bases the server already has). Git verifies a packfile-URI
  pack by its hash, so a thin pack can't be thickened client-side and can't be served as a URI.
  When a packfile-URI fetch plans a thin L0 pack that has no twin yet, the server writes a *thick
  twin*: the same objects, external bases expanded, OFS deltas only, as a new immutable pack
  (`twin/R/<pack>`); the original stays byte-identical, and all later URI clients reuse the
  twin. URI clients thus get every planned pack by URI and an empty inline pack (checked in
  tests/cg6.sh). If building fails, that pack's objects go inline (always correct).
- First version built twins eagerly after every thin push. Measured: a twin holds full copies of
  the delta bases, so for small edits to large files twins cost ~18x the L0 bytes. On demand,
  only the uncovered tail of each ref is twinned in practice (covers are already self-contained):
  3 twins instead of 300 in tests/cg6.sh.
- The article writes a subset of commits synchronously and the rest asynchronously for very large
  pushes. This design writes no per-commit metadata at all: haves are recognised by ref-version
  tips only (D7), so a push costs <= 3 reads + 4 writes per ref whatever its size
  (tests/metrics.sh: 1 commit, 1,000 commits and a 20 MB blob all cost 4 reads / 4 writes).
  Cost: a client whose only common commits are non-tip intermediate commits gets more data.

## D14 What scaling by adding servers, metadata units and blob stores means here (the article's description, paraphrased; https://openai.com/index/scaling-git-at-openai/)
- Stateless parts: HTTP front ends, negotiation/planning, pack splicing and the cache nodes hold
  only derived caches of immutable data (D6), so any number of them can run against shared
  stores. The cache tier already uses consistent hashing over N nodes.
- What is NOT done: the metadata store is embedded in the server process (one process per
  `G_DATA_DIR`, as the harness requires) and the blob store is a local directory. Running several
  front ends needs the metadata store behind a network API (its interface is just
  get/scan/txn-commit with per-key versions, see meta.rs) and an S3-like blob store; keys are laid
  out per `(repo, ref)` so the store can be range-sharded into units without cross-ref
  transactions (only `--atomic` multi-ref pushes span keys of several refs).

## D15 Directory/file ref conflicts
- Like git, creating `refs/heads/a/b` is rejected while `refs/heads/a` exists and vice versa
  (`cannot lock ref …: '…' exists`). The checks are point/range reads OUTSIDE the transaction's
  read set: putting a shared parent key (e.g. `refs/heads/feature`) into every creator's read set
  would make all creations under that directory hold the same stripe lock through the WAL fsync —
  a shared lock between independent branches. Residual race: two concurrent pushes creating `a`
  and `a/b` can both succeed (clients then cannot fetch both into one repository, as with any
  D/F conflict). Namespace roots (`refs/heads`, `refs/tags`, …) are never checked as parents.
