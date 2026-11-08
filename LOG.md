# LOG

Times are local session time (2026-11-06).

## Session 1
- 01:55 Read BRIEF/INTERFACE/gates. Found `reference/openai-git` = modified client (2.56-dev) with
  `explicit-haves` receive-pack capability (Git-Protocol extra parameter `explicit-haves` on the
  receive-pack discovery GET) and `no-ref-delta` + parallel packfile-uri indexing on fetch.
- 02:05 Environment: git 2.54 (Apple), cargo 1.98, crates.io reachable, no PostgreSQL. Decided on an
  embedded metadata store written in Rust (see DECISIONS.md D1).
- 02:10 Created `server/` cargo project (tokio, hyper 1, flate2, sha1, serde_json, libc).
- 02:20-03:05 Wrote server: pack.rs (codec, sidecar .pidx), ingest.rs (streaming single-pass
  decoder teeing into blob staging file), meta.rs (OCC KV + WAL group commit), blob.rs,
  repo.rs (ref versions, push commit, fetch planner), packout.rs (inline pack splicing),
  proto.rs (receive-pack v0, upload-pack v2), http.rs (hyper), main.rs (start/stop/create-repo).
  Build issues: edition-2024 reserved `gen` identifier; one borrow error. Fixed.
- 03:10 tests/cg1.sh (23 checks: empty clone, push, clone, incremental fetch, branches, deltas,
  tags incl. annotated/peeled, delete, fsck --full --strict): ALL PASS first run.
- 03:20 tests/cg2.sh (same-ref races on trunk and non-trunk, force-with-lease, --atomic, SIGKILL
  after ack, SIGKILL mid 30MB push, 3 kill cycles with 8 concurrent pushes each): ALL PASS.
  First version of the kill-cycle test was useless (clones were killed before pushing, 0 acked);
  rewrote it to pre-clone, now ~9/24 pushes acked per run and none lost.
- 03:35 tests/cg3.sh (empty-pack branch at non-tip commit, duplicate content, delete/recreate,
  force push, cherry-picked blob from another branch, nested annotated tags, partial clone ->
  full fetch, structural checks: no objects/ dir, no loose objects, packs byte-identical +
  read-only, maintain does not modify packs, restart): 30/30 PASS (one test-script bug fixed).
- 03:50 tests/cg4.sh N=1000 P=200: all pushes OK with read-your-own-write, 0 metadata conflicts,
  ~5 metadata reads + 4 writes per push, hot-ref races (8 writers) exactly one winner, polling
  reader never sees a ref go backwards. 70 s wall (client-bound; server CPU 4.2 s).
  Changed metadata read accounting: a range scan counts as 1 read op (keys reported separately
  as metadata_keys_scanned); before, the all-refs advertisement made reads O(#refs) per push.
- 04:00-04:30 CG7 simulation mode (`pando sim --seed N`): seeded single-threaded scheduler over
  synthetic clients driving the real receive-pack/upload-pack handlers, ingest, store, planner;
  faults: metadata commit failure before WAL fsync, crash between fsync and apply, crash/restart
  with optional torn WAL tail; compaction runs. Invariants: closure completeness of every ref,
  CAS model (accept iff old==model tip), durability of acked pushes, monotonic reads per client,
  fetched packs index + connectivity on the client, pack immutability.
  First run found "unacked push visible after restart" violations: caused by my fault model
  (after-fsync failure continuing without a crash). Fixed the model (after_wal => immediate
  crash) and hardened meta.rs: a WAL write/fsync error now aborts the process instead of
  returning an error (memory/disk divergence otherwise possible).
  8 seeds x 400 steps: 0 violations. Same seed => byte-identical trace.
- 04:35 compact.rs: covers L1..L4 (fanout 4), background compactor notified on aligned versions
  + catch-up at start + `maintain`. Sim seeds 11-13 x 600 steps with covers: 0 violations.
- 04:45 cache.rs: 3 cache nodes on ephemeral ports, consistent hashing (64 vnodes), LRU, blob
  fallback; packfile-uris for self-contained packs (covers, non-thin L0).
- 04:50 tests/cg6.sh (V=300 single-commit pushes): covers L1..L4 built in background (98),
  all 300 L0 packs retained, planner steps grow 1,2,1,2,4,5,7,8,10 for distance
  1..256 (log), clone with fetch.uriprotocols=http fetched 6 cover packs from cache nodes with an
  empty inline pack; stock clone inline. 17/17 PASS.
  Note: plan_metadata_reads is ~17 regardless of distance: dominated by tip lookups for the
  client's first 16 haves; RV/cover records are immutable and served from the in-process cache.
- 05:00 Built reference/openai-git (copy in $TMPDIR/ogit, `make NO_GETTEXT=1 NO_PERL=1
  NO_PYTHON=1 NO_TCLTK=1`, ~3 min). tests/cg5.sh with it: explicit-haves push deps trunk only,
  second topic push re-sends topic commits, no-ref-delta honoured (advertised to explicit-haves
  clients), non-ff/stale-lease rejected, fresh lease ok, delete/recreate, race exactly one winner,
  parallel packfile-uri clone (fetch.packfileUriJobs=4, no-ref-delta) with 10 URI packs. 17/17 PASS.
- 05:15 v0.rs: protocol v0/v1 upload-pack. tests/features.sh (27 checks: v0/v1 clone+fetch,
  follow-tags/auto-follow, bare/mirror, push --mirror, prune, push -o, dry-run, atomic, symref,
  gitlinks, 20MB object, gzip negotiation with 300 local commits, fetch by sha, filter fallback,
  shallow -> clear error, dumb http 403, 404): ALL PASS. Wrote FEATURES.md, LAYOUT.md, MODEL.md,
  DECISIONS D5-D11.
- 05:30 WAL checkpointing (background, >64MB) + sim exercises checkpoint with crashes/torn tails:
  6 seeds x 500 steps, 0 violations. `version` subcommand; run.sh rebuilds if binary unusable.
- 05:40 tests/run_all.sh full regression: cg1..cg6 all pass; features.sh 2 failures that turned
  out to be CLIENT-side: git's detached auto-maintenance (cruft packs + MIDX) raced with 300 rapid
  local commits and left a local ref pointing at a pruned object. Disabled gc.auto/maintenance.auto
  in tests/lib.sh; also fixed lib.sh so a failing script exits non-zero. features: 27/27.
- 05:55 shallow.rs: protocol v2 `deepen N` (fresh pack via object walk), shallow-info with
  shallow/unshallow. First version stopped the depth walk at haves -> `fetch --depth 4` and
  `--unshallow` returned nothing new; fixed by ignoring haves in the depth walk. features.sh 32/32.
- 06:05 v2 `want` of any object in a committed pack (falls back to the introducing ref version);
  features.sh 33/33. CG2 + crash during background compaction: 25/25.
- 06:15 Hardening for the CG7 attack team: untrusted pack object count no longer used for
  pre-allocation; ingest delta-chain recursion bounded (and self-referential REF bases refused);
  cache-node path components validated. Added `pando fuzz --seed N --iters K` (bit flips,
  truncation, junk insertion, absurd counts, recomputed trailers, broken pkt framing through
  handle_receive): 3 seeds x 1500 iterations, 0 panics, 0 broken refs afterwards.
- 06:30 tests/metrics.sh: push metadata ops 4-5 reads / 4 writes for 1 commit, 1000 commits, 20 MB
  (first version of the check was too strict: first push also loads the object index; relaxed).
- 06:45 Thick twins (D13): background self-contained copy of every thin L0 pack; URI clients now
  get an empty inline pack right after thin stock pushes. cg6.sh 20/20.
- 07:00 Sim invariant for twins (same objects, self-contained): 4 seeds, 0 violations.
  Full regression (run_all.sh): all suites pass, 5 sim seeds clean.
- 07:05-07:20 tests/stress.sh: 40 writers x 5 rounds (+ trunk races) with 8 readers looping
  mirror-clone + fsck --strict + fetch + fsck: 0 errors, all final pushes visible. (The script's
  wait loop never ended — `jobs -p` counting bug — so it ran ~10 min of reads until stopped by
  hand; the server served 870 MB without a single failed clone/fsck. Fixed the script.)
- 07:30 Reasoned about MODEL assumption A1 with multi-ref packs: a later push depending on ref b's
  objects in a pack shared with ref a was recorded as depending on a's version only; a client
  holding a's tip that ran `gc --prune=now` would then miss b's objects. Fix: Pack.rvs is
  append-only (every ref version introduced with the pack, across the per-ref transactions of a
  non-atomic push) and dependencies include all of them. New cg3 case (multi-ref push,
  single-branch clone, gc --prune=now, fetch branch built on the other ref): PASS.
- 07:40 Extended the simulator with multi-ref pushes (25% of pushes) and client gc (prune to
  reachable). It immediately found 2 issues on seeds 5/7/9:
  (1) SERVER BUG: thin-delta base kept thin because the base's pack's *first* introducing version
      was held — wrong for multi-ref packs after client gc ("thin base not available"). Fixed:
      client_holds requires all introducing versions held.
  (2) SIMULATOR BUG: its client pack decoder could not resolve an OFS delta whose base is a
      forward REF delta (valid git; index-pack handles it). Rewrote it to resolve iteratively.
  Seeds 1-16 now clean.
- 07:50 40-seed sweep (600 steps each) with multi-ref pushes + client gc: 0 failing seeds.
- 08:00 Simulator now also exercises packfile-uris (inline indexed first, then each URI pack
  without thickening + hash check) and no-ref-delta (all packs standalone, no REF_DELTA).
  10 seeds x 600 steps, ~320 URI fetches: 0 violations.
- 08:15 Full regression found 3 shallow failures ("unresolved deltas left after unpacking" on
  fetch into a depth-1 clone): REAL BUG — a shallow client's have was treated as holding the
  whole linked history, so deltas were kept thin against objects the shallow client never had.
  Fix: if the client sends `shallow` lines, only its exact have versions are held and no delta is
  kept thin. features.sh 3/3 runs 33/33.
- 08:25 tests/trace_check.py (MODEL §6 recipe). Found a TRACE BUG: ref_version_committed carried
  the version number in `seq`, which the logger overwrites with its counter -> renamed `version`.
  Checker tolerates version gaps only across a restart (WAL-durable but unacknowledged commits
  recovered without an event) and dependencies on such recovered versions. CG2 trace (with races
  and SIGKILLs) and 4 simulator traces: 0 violations. Added to run_all.sh.
- 08:35 `_stats.packs_stored` now counts every pack written (L0 + covers + twins; L0 and derived
  also reported separately). LAYOUT.md notes that stock L0 packs are thin as sent.
- 08:40 tests/perf.sh: reference/git tree imported (+200 edit commits): push 13 MB pack 2 s,
  clone+fsck 1 s, URI clone after maintain 1 s, server CPU 0.39 s total. PASS.
- 08:50-09:20 tests/model_check.py: exhaustive BFS checker of the abstract protocol (MODEL §8).
  First runs found nothing — mutation testing showed the model was too weak (no cross-ref or
  root commits; thin bases only on even ids). After enriching it, it caught both earlier bugs
  as mutants AND found a NEW REAL DESIGN BUG at depth 8: in a multi-ref push, a ref was marked
  "linked" if its old tip was a boundary commit of the pack, even when that boundary belonged
  to the other ref (e.g. force main to unrelated history + create topic on old main in one
  push) -> a client holding new main was assumed to hold old main's history. Fixed in model and
  server (links_to: walk this ref's commits within the pack). Correct model: 1.29 M states, 0
  violations. Added the exact scenario to cg3 with real git (passes; main@4 linked:false).
- 09:30 Model checker extended with covers (planner jump rule) and CAS deletes; configurable
  spans. Child-cover indexing bug in the checker itself (only worked for default spans) fixed.
  depth 8 (covers+deletes, spans 1,2): 5.05 M states, 0 violations; 4 mutants all detected.
  Added to run_all.sh.
- 09:40 DECISIONS D4/D5/D7 updated for the multi-ref, per-ref linking and shallow fixes.
  cg2 extended with create/create, delete/update, force/update and tag-create races (exactly one
  winner each, mirror fsck after): 36/36 PASS.
- 09:55 Offline rebuild check: fresh copy without target/ and without CARGO_HOME builds with
  run.sh (uses ../.cargo-home) in 11 s. Compiler warnings cleaned.
- 10:00 D/F ref conflicts rejected like git (features.sh 36/36). First version read the parent
  key inside the transaction -> would make every branch creation under a directory share a stripe
  lock through the fsync (violates "no shared lock"); changed to reads outside the read set
  (D15). cg4 N=500: 0 conflicts.
- 10:10 Metadata commit-lock stripes 256 -> 4096 (unrelated pushes collided on a stripe ~6% of
  the time and then waited one extra fsync). Full regression: all suites, traces, 5 sim seeds,
  model check depth 7 and all 4 mutants detected — GREEN.
- 10:20 Server raises its RLIMIT_NOFILE soft limit at start (harness shells may start at 256).
  cg4 N=600 P=300 run under `ulimit -n 256`: PASS.

## Status at end of session 1 (for a later session)
- All visible gates CG1-CG6 pass by my tests; CG7 artifacts provided (MODEL.md, event log +
  tests/trace_check.py, `pando sim`, tests/model_check.py with mutants, `pando fuzz`).
- Regression: `tests/run_all.sh` (needs the modified client built in $TMPDIR/ogit for CG5;
  build recipe in README.md). Everything green at 10:10.
- Known gaps / ideas not done: covers do not recompute deltas (no compression gain, D9);
  include-tag ignored (D7); haves recognised only at ref-version tips (D7/D13); metadata store
  embedded (D14); shallow only via depth (D12); no auth/TLS; D/F conflict race (D15).
- Bugs found and fixed during the session (details above): fsync-failure divergence (sim),
  multi-ref pack dependencies (reasoning), thin-base holds for multi-ref packs (sim), shallow
  client held ranges (regression), trace `seq` clobbering (trace checker), per-ref "linked"
  in multi-ref pushes (model checker), D/F check creating a shared stripe (review).
- 10:30 Measured cover sizes vs members (cg6 trace): L1 5.9x, L2-L4 ~1.0x — L2+ were built from
  child covers, so each child's full chain-head copies were carried upward: the L4 cover a clone
  uses held 64 full copies of the frequently edited file (509 KB). Now every level is built from
  the member versions' L0 packs: L4 71 KB (0.8x of members), L3 1.04x, L2 2.0x; L1 overhead is
  inherent to self-contained packs (one full copy per chain per cover).
- 10:40 Thick twins were ~18x the L0 bytes for this workload (full copies of delta bases).
  Switched from eager (after every thin push) to on-demand (first packfile-URI fetch that needs
  one): cg6 now builds 3 twins instead of ~300, URI clients still get an empty inline pack.
  cg6 20/20.
- 10:50 Inline packs: whole objects from covers are replaced by their L0 delta when the base is
  already emitted / held (cover chain heads). cg6 inline bytes now ~250 B/version at every
  distance (was ~6 KB extra per cover used). 12 sim seeds (233 fetches with substitutions) and
  full regression: GREEN.
- 11:00 Background verification: model check 3 refs depth 6 (covers+deletes): 415 k states, 0
  violations; 100-seed simulator sweep (seeds 200-299): 0 failing seeds.
- 11:10 Simulator now performs true explicit-haves pushes (only `.have` objects as prerequisites,
  `explicit-haves` requested). 16 seeds, 639 explicit-haves pushes: 0 violations.
- 11:20 Final run_all: sim seed 4 failed the TRACE CHECK (not the sim invariants): dependencies on
  b3@19, a WAL-durable-but-unacknowledged version recovered after a crash that was b3's LAST
  version, so no later event exposed the gap. Checker limitation; it now accepts a dependency on
  an unobserved version only if a restart happened after that ref's last observed version.
  Seeds 1-30: 0 trace violations.
- 11:35 FINAL: tests/run_all.sh exit 0 — cg1 23/23, cg2 36/36, cg3 35/35, cg4 9/9, cg5 17/17,
  cg6 20/20, features 36/36, all traces clean, sim seeds 1-5 clean, model check clean, 4/4
  mutants detected. Budget used ~$28 of $85.
