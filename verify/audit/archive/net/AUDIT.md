# Independent audit: Pando implementation (snapshot in `impl/`)

Build: `work/impl` is a copy of `impl/` (made writable; `impl/` untouched). `cargo build --release` offline succeeds.
The environment sets `CARGO_TARGET_DIR=work/target`, so the binary was copied to `work/impl/server/target/release/pando`,
where `run.sh` expects it.

Reproducers are in `work/repro/`; each run's output is saved next to it as `rNN*.out`.
- `rlib.py` holds the shared helpers: the builders' `distlib.Cluster`/`Meta`/`Minio` (imported read-only), a frame-aware fault-injecting TCP proxy for the metadata protocol, and a direct metadata-protocol client.
- Each script exits 1 and prints `DEFECT REPRODUCED` when the defect shows on this snapshot.
- Run one with `cd work/repro && python3 rNN_….py`; `rlib.py` sets `G_RUN_SH`, `G_CLIENTS` and `G_MINIO` itself. Set `KEEP=1` to keep the data directories.

Code citations are `impl/server/src/<file>:<line>`.

**Environment limit.** The sandbox forbids binding Unix-domain sockets (`EPERM`, checked). So every multi-frontend run here uses the metadata service over **TCP** (`meta-serve --listen tcp:`), with either a shared pack directory (`PANDO_BLOBS`) or MinIO (`PANDO_S3`). The builders' reference mode (Unix socket plus shared directory) could not be executed. The frontend code paths are identical apart from the socket type (`meta.rs:689-745`).

## Summary table

| # | Finding | Severity | Evidence | Reproducer |
|---|---------|----------|----------|------------|
| F1 | A metadata-service outage over 10 s while the background compactor holds `MAINT_LOCK` panics the compactor (`meta.rs:777` panics on `Unavailable`; `compact.rs:216,223`) and poisons the lock. From then on that frontend builds no covers or self-contained copies, and `run.sh maintain` returns 500 (`PoisonError`), until the process restarts. Pushes are still accepted. | **major** | executed | `repro/r01_meta_outage_poisons_compactor.py` → `r01.out` |
| F2 | S3 publication treats `412` as success after checking only the **size** of an index, or the size plus the last 20 bytes of a pack (`blob.rs:172-187`). If the key already holds different bytes, the push is acknowledged anyway. The publishing frontend serves its local copy; any other frontend cannot serve the ref (clone fails). This is an acknowledged ref whose objects are not in the store as pushed. Trigger: a key already holding other bytes, e.g. a foreign or buggy writer, an older version, or store corruption. Between Pando frontends, same-size different-content indexes do arise (a thin pack's external-base pack names can differ), but both copies are then valid. | **major** (defence-in-depth gap on the ack path) | executed | `repro/r03_s3_412_differing_content.py` → `r03.out` |
| F3 | Shared-directory mode: the startup sweep (`blob.rs:77-91`) treats an instance area with no LOCK yet, or an unlocked LOCK, as dead. Its owner creates the directory (`blob.rs:93`) before taking the flock (`blob.rs:94-95`), so a concurrently starting frontend can delete a **live** frontend's area. That frontend passes health checks (pid file, port open) but rejects **every** push (`No such file or directory`) until restarted; sometimes it fails to start instead. In 100 concurrent starts: 4 live frontends with no area, 1 failed start. Contradicts `docs/DISTRIBUTED.md:33-34`. | **major** (availability, silent) | executed | `repro/r06_shared_tmp_sweep_race.py` → `r06.out` |
| F4 | Non-atomic multi-ref push: when a later ref's commit fails (e.g. lost reply), `push_inner` returns `Err` (`repo.rs:840`). `handle_receive` then reports **every** command as `server error` (`proto.rs:253-256`), including refs already committed whose replies were received. | minor | executed | `repro/r02_lost_reply_and_meta_restart.py` (b) → `r02.out` |
| F5 | A commit whose reply is lost is reported as `[remote rejected] … commit outcome unknown` (`meta.rs:835-843`), but the ref did move. This is honest and documented, but stock Git records it as a rejection. A retry reports "Everything up-to-date". | minor (by design) | executed | `r02…` (a) |
| F6 | A stalled object store (process alive, not answering) holds a push for 120 s: per-attempt read timeout 120 s (`s3.rs:289`), connect without timeout (`s3.rs:285`), and the 10 s retry deadline is checked only between attempts (`s3.rs:249`). A stalled metadata service hangs frontend requests **indefinitely**: the pooled connection has no read or write timeout (`meta.rs:733-753`). Both recover when the stall ends. | minor (liveness) | executed | `repro/r05_store_outage_and_stalls.py` (b),(c) → `r05.out` |
| F7 | A snapshot keeps only live keys (`meta.rs:463-466`), and `open` recovers the next LSN as the max version of live keys (`meta.rs:115-144`). If the newest commits deleted keys, the LSN goes **backwards** after checkpoint plus restart (74 → 72 observed). LSNs are then reused: a reader holding the old mark misses new changes (`repo.rs:342`), and versions used for OCC can repeat (ABA). **Latent**: no server code issues deletes (no `.delete(` call in `src/`), but the wire protocol accepts them (`meta.rs:912`) from any unauthenticated TCP client. | minor (latent) | executed at protocol level | `repro/r08_meta_checkpoint_restart.py` → `r08.out` |
| F8 | A frontend that cannot read an index or pack while serving a fetch writes `ERR …` as a bare pkt-line into a side-band stream (`http.rs:289-292`). The client prints `protocol error: bad band #69` instead of the reason. | minor | executed (seen in R03) | `r03.out` |
| F9 | Downloaded `.alt` substitution maps are **not verified at all** (`blob.rs:208-226`: `_ => {}`), and a failed fetch is cached as "no map" for the process lifetime (`blob.rs:423-427`). Downloaded indexes are checked only for their own checksum and id, not against the pack. Packs are not checked against the index size. Contradicts `docs/NETWORK-STORAGE.md:36-41` ("verifies it … size matches the index"). | minor | read only | – |
| F10 | Duplicate background work across frontends: cover bytes are deterministic, but the alt map depends on each frontend's object-index registration order (`repo.rs:397` `or_insert`; `compact.rs:327-338`). Two frontends building the same cover can produce `.alt` objects of different sizes. The second then fails `publish` with "refusing to proceed" (`blob.rs:178-180`), so `maintain` errors on that frontend. Permanent for that cover if the first builder died between alt publication and the cover commit. Losers' cover and twin packs stay as orphans (no GC). | minor | read only | – |
| F11 | Networked-mode fidelity: the main listener and cache nodes bind `127.0.0.1` (`main.rs:248`, `cache.rs:139`), and packfile URIs are hard-coded to `http://127.0.0.1:<port>` (`cache.rs:51`). A frontend on another host can't be reached, and A8 packfile-URI fetches can't work for any client off the host. | minor (fidelity) | read only | – |
| C1 | Control: acknowledged-write durability under SIGKILL storms (metadata service, MinIO, frontends; 9 kills; about 730 push attempts in tcp-s3 and 630 in tcp-fs mode). Every acknowledged tip was present, and mirror clones from both frontends passed fsck. | – (no defect) | executed | `repro/r07_kill_storm_durability.py` → `r07.out`, `r07-fs.out` |
| C2 | Control: SIGKILL plus restart of `meta-serve` under live frontends. No acknowledged ref was lost; the first push after restart succeeded (stale pooled connections were replaced by the push's earlier reads); a push through the other frontend depending on the first's objects succeeded. | – | executed | `r02…` (c) |
| C3 | Control: the bundled MinIO honours `If-None-Match: *`, including in 40 concurrent create races (never two 200s). | – | executed | `repro/r04_minio_conditional_put.py` → `r04.out` |
| C4 | Control: a store outage during a push gives no acknowledgement and the ref doesn't move; it fails after 10.4 s, and after restart the push and a clone through the other frontend succeed. | – | executed | `r05…` (a) |
| C5 | Control: meta-serve checkpoint plus SIGKILL plus restart (without deletes) keeps every committed key. | – | executed | `r08…` |
| C6 | Control: same-ref compare-and-swap across two frontends (tcp-s3), 6 contending workers, with a metadata SIGKILL partway through. 116 acknowledged updates: none shares an `old` (no lost update), every acknowledged tip is an ancestor of the final tip, and the clone passes fsck. | – | executed | `repro/r10_same_ref_cas_across_frontends.py` → `r10.out` |
| F13 | A4 deviation: pushes to **different refs** that set the same new tip or carry the same pack share `tip/<oid>` and `pack/<id>` in their transaction (`repo.rs:749-752, 773-774`), so they contend. A push to `refs/heads/other` waited 2.5 s behind a commit to `refs/heads/slow` stalled in the service; a control push with its own tip took 0.02 s. Correctness is preserved (conflict, then retry). | minor (fidelity) | executed | `repro/r11_a4_shared_tip_contention.py` → `r11.out` |
| F12 | The builders' own `D2StalledCommit` (`verify/dist/test_dist.py:104-131`) **fails intermittently** on this snapshot in tcp-s3 mode: 2 of 4 runs. A push to an unrelated branch through frontend B waited 2.4 s and 5.4 s, finishing exactly when the stalled or rival commit finished. In tcp-fs mode it failed 0 of 3, and my replay (`r09`) 0 of 6 in tcp-s3. The cause was **not established**. It may be an occasional A4 violation (unrelated pushes contending) or a host-load artefact. | minor (unexplained) | executed (builders' test) | `work/builders-dist-tcp-s3.txt`; `repro/r09_d2_unrelated_push_waits_s3.py` → `r09.out` |

No **blocker** was found. Every SIGKILL test kept every acknowledged write. The majors are: acknowledging a push whose stored objects differ from what was pushed (F2), a silent loss of a frontend's write availability (F3), and a permanent loss of background compaction after one outage (F1).

## Focus 1: acknowledged-write durability

**Order of operations per mode** (read, then exercised by C1, C2 and C4):

1. **All modes, ingest.** The pack is streamed to `tmp_path()` and fsynced (`ingest.rs:103,210-211`); the index is built in memory.
2. **Single process / shared directory (`install`, `blob.rs:305-339`).**
   - `ensure_durable_dir` fsyncs every directory from the pack directory up to the root, once per process (`blob.rs:272-296`).
   - The index tmp file is written and fsynced (`blob.rs:316-323`).
   - `link_no_replace` publishes pack then index (`blob.rs:326-327`); both are made read-only, then the pack directory is fsynced (`blob.rs:337`).
   - If the files already exist, the directory is fsynced again (`blob.rs:310-315`).
3. **S3 (`PANDO_S3`).**
   - The same local links go into a disposable cache, without fsync (`blob.rs:320, 343-348`).
   - `publish_pack` PUTs the pack, then the index, create-only (`blob.rs:352-364`); a 412 goes through the verify path (F2).
   - The metadata commit happens only after both PUTs return.
4. **Metadata commit (`repo.rs:802`).**
   - Local: the WAL record is appended and `sync_data`'d by group commit (`meta.rs:304-319, 350-386`), applied, and only then returned (`meta.rs:326-347`).
   - Remote: the service runs the same `commit` (`meta.rs:915`) and writes the reply only after it returns (`meta.rs:929`). The frontend does not retry a commit (`meta.rs:808-844`).
5. **Report.** `ok <ref>` is produced only from `CmdResult.ok`, which is set only after `Ok(lsn)` (`repo.rs:805-836`, `proto.rs:273`). No ref is acknowledged before its pack, its index and the metadata transaction are durable, in any mode.

**Windows found:**
- None that loses an acknowledged ref (C1, C2).
- F2 is the window where an **acknowledged** ref's pack or index in the store is not the pushed one.
- F4 and F5 are false *negatives* (reported failure for a committed update), not false acks.

**Assumed, not enforced:**
- That the object store's 200 means durable. MinIO single-drive durability is MinIO's; the builders say so (`docs/NETWORK-STORAGE.md:17`).
- That the shared directory honours `link(2)` and `fsync` semantics, and `flock` across all frontends (on NFS, `flock` may be host-local, which makes F3 worse).
- `F_FULLFSYNC` semantics on APFS.
- That the WAL tail is the only place corruption occurs: `parse_records` stops at the first bad CRC and `open` truncates **everything after it**, silently (`meta.rs:587-605, 152-155`). A corrupt record mid-WAL drops later acknowledged commits without an error. Read only; needs media corruption.
- Power loss was not tested (not possible here).

## Focus 2: S3 publication semantics

- **Conditional create.** Every PUT carries `If-None-Match: *` (`s3.rs:310-317`). 200 means created, 412 means it existed; anything else fails the push. MinIO honours this, atomically (C3).
- **Retries and timeouts.** Transport errors and 5xx are retried with backoff until about 10 s (`s3.rs:238-259`); C4 showed failure at 10.4 s. A retried create whose first attempt succeeded unseen resolves through 412-and-verify, which is sound **only if** verification is content-exact; it is not (F2). Stalls: F6.
- **Partial uploads and lost replies.**
  - The body is signed with its SHA-256 (`s3.rs:264-268`), so the store rejects a truncated body.
  - A response that arrives early (412 before the body is read) is still parsed (`s3.rs:290-306`).
  - A lost 200 becomes 412 on retry, then verify.
- **Two frontends publishing the same object.** One 200, the other 412 then verify, which is fine for identical content. For **different content under the same key** (index or alt maps built by different frontends, F10; or any foreign writer, F2), the second either aborts (size differs) or silently accepts (same size, different bytes).
- **Pack before index** (`blob.rs:357-363`): the index is the completion marker. Readers locate packs only through committed metadata, and the commit follows both PUTs. A crash between the PUTs leaves an unreferenced pack (builders' N3 hook, `blob.rs:358-362`).
- **Overwriting.** Nothing in the client issues an unconditional PUT or a DELETE (`s3.rs`), so a published object can't be overwritten *by Pando*.
- **Absent objects.** A ref can't be published whose objects are *absent* from the store. One can be published whose stored objects are **wrong** (F2, executed).
- **Error responses the client might misread.** `put_if_absent` accepts any 412. A 412 caused by something other than an existing key (another precondition, or a proxy) would be read as "exists" and go to HEAD; a 404 there fails safe (`blob.rs:177`). A GET that ignores `Range` returns 200 with the full body; the trailer comparison then fails safe ("refusing to proceed").

## Focus 3: cache correctness

**S3 mode.**
- The local cache is wiped at every start (`blob.rs:131-138`) and filled write-once from verified downloads: tmp file, then `link` without replace (`blob.rs:152-164, 191-233`).
- A process crash can't leave a torn **linked** file, because the file is complete before the link. Power loss is covered by the wipe.
- Concurrent fills: each filler verifies its own copy; the second link loses harmlessly (`blob.rs:161`).
- Verification is strong for packs: SHA-1 of the content equals the trailer equals the id (`blob.rs:209-218`). It is weak for indexes (internal checksum plus id, `pack.rs:357-366`, `blob.rs:219-224`) and **absent** for `.alt` maps (F9).
- A locally installed but **unpublished** pack (push failed at PUT) stays in the cache, but nothing references it. A repeat push of the same pack republishes it (`blob.rs:310-315`).
- The cache-node HTTP path validates repository and id strings (`cache.rs:96-109`).

**Shared-directory mode.** The tmp-area sweep has the race in F3 (executed). There is no periodic sweep, so a crashed frontend's area stays until the next frontend start.

**Single process.** Everything under `tmp` is removed at start (`blob.rs:105-115`).

## Focus 4: distributed consistency

- **Per-ref CAS.**
  - The ref key is read in the transaction (`repo.rs:664`) and validated by version under striped locks at the service (`meta.rs:283-300`).
  - A version is the LSN of the last write, so it is unique unless LSNs regress (F7, latent).
  - The read set also contains `tip/<oid>` and `pack/<id>` (`repo.rs:751, 774`). Pushes to **different** refs that share a new tip or the same pack conflict and retry; this deviates from A4's "only contended key is the ref pointer" (F13, executed).
  - The directory/file conflict check is deliberately outside the read set; the residual `a` vs `a/b` race is documented by the builders (D15, `repo.rs:678-683`). Not executed.
  - Executed: C6 (same-ref contention across frontends with a service SIGKILL; no lost or duplicated update) and C1 (distinct refs).
- **Change log and stable LSN.**
  - `finish_lsn` advances `stable` only over contiguous finished LSNs (`meta.rs:388-398`).
  - `changes_since` returns every applied record above the mark, including ones above `stable`, and the reader advances only to `stable` (`meta.rs:421-443`, `repo.rs:340-374`). This handles out-of-order application and retries; refreshes are idempotent (`repo.rs:382-399`).
  - First build: `stable` is read before the scan (`repo.rs:346`).
  - Service restart: `stable` = max recovered LSN, which is at least any mark a frontend holds, *unless* deletes regress it (F7).
  - Executed: C2 (no missed pack, no refused valid push after a service SIGKILL) and C5.
- **Commits with a lost reply:** F5 and F4.
- **Stale visibility:** a push is acknowledged only after the service applied it, and a frontend whose index misses an object refreshes once before declaring it missing (`repo.rs:409-431`). No stale read of an acknowledged push was observed (C1, C2).
- **Duplicate background work:** covers and twins are write-once keys checked inside the transaction (`compact.rs:79-80, 357-359`); losers' packs become orphans. F10 covers alt divergence. The background compactor's fragility under outages is F1.
- **Concurrent maintenance on one frontend** is serialised by `MAINT_LOCK` (`compact.rs:189-231`); a single panic poisons it permanently (F1).

## Focus 5: test coverage inventory and gaps

The full per-file, per-test inventory is in **`work/coverage-inventory.md`** (built by a delegated read; I spot-checked `p1/test_p1.py:179`, `tests/cg2.sh:78` and `dist/test_dist.py:8-9,214`, and they are as described). Unit tests were run: **11 passed, 0 failed** (`work/unit-run.txt`). Highlights:

**Vacuous or weak tests:**
- `verify/p1/test_p1.py:179` asserts `built <= thin | built`, which is always true.
- `tests/cg2.sh:78` "no temp blobs left referenced" only tests that the directory exists.
- `verify/subst/equiv.py` exits 0 even when it detects a difference.
- `meta.rs:1043-1096` (randomised out-of-order) never asserts that reordering actually happened.
- The SHA-256 padding loop (`s3.rs:396-398`) has no assertion.

**Silent skips:**
- D5 in S3 mode (`dist/test_dist.py:214`).
- The fork client is dropped when `FORK_GIT` is absent (`dist/test_dist.py:8-9`).
- P1Recompress is "INCONCLUSIVE" (`p1/test_p1.py:76`).
- `run_all.sh` skips cg5.

**Pass for the wrong reason, possibly:**
- Many assertions rest on the server's own event log or `/_stats` counters, or on timing.
- `tests/trace_check.py` excuses version gaps across restarts.

**No test for:** the findings above (F1–F10); mid-commit SIGKILL of the metadata service; lost commit replies; SIGKILL of MinIO; a 412 with different content; corrupt `.pidx` or `.alt` downloads; meta-serve checkpoint (64 MiB threshold never reached); stable LSN against a *remote* service across a restart; concurrent cache fill; concurrent duplicate cover builds across frontends; metadata or store **stalls**; the tmp-sweep race.

Mapping the focus areas to tests:
- (1) is covered for SIGKILL of a single process or a frontend (CG2/CG6/P0/D5).
- (2) only by N2 (sequential and concurrent same-content publication; the overlap is not forced), N3 and N4.
- (3) by N5 (cold cache) and D5/D6 (sweep after a dead owner, never a concurrent start).
- (4) by D1–D8, I1–I3 (local-store LSN unit tests) and `concurrent_copy_builds_publish_one_copy` (single process).

**Guards against tests being deleted or not run:**
- `verify/run_suites.sh` exits 2 at its manifest checks (`:16-17`) here, before any suite runs, because `clients/heldout` has no manifest and `~/pando-eval/cg56` is absent.
- The unit-test floor (`verify/unit_min.txt` = 11 = today's count) only prints a message when breached; the exit code never changes.
- The Python suites have no count guard and their exit codes are discarded.
- Nothing stops a test from being deleted unnoticed.

**Builders' suites, run by me on this snapshot:**
- Rust unit tests: 11/11 pass (`work/unit-run.txt`).
- `verify/net/test_net.py`: 6 passed (`work/builders-net.txt`).
- `verify/dist/test_dist.py` with `G_DIST_MODE=tcp-s3`: 9 passed, 1 skipped (D5, by design), **1 failed** (`D2StalledCommit`, intermittent; see F12) (`work/builders-dist-tcp-s3.txt`).
- `verify/p0/test_p0.py` (single process): 4 passed (`work/builders-p0.txt`).
- `verify/p1/test_p1.py` (single process): 2 passed, 3 failed (`work/builders-p1.txt`):
  - `P1Copies::test_thin_push_fetchable_before_copy_then_served_by_uri`: environmental. It needs the fork client under `~/pando-eval`, which the sandbox denies.
  - `P1Recompress::test_sigkill_during_recompression…`: a **test race**, not a server defect. The leftover `blobs/tmp/in-83635-1-…` has counter 1, so it was created by the *restarted* process's startup catch-up compaction (`compact.rs:170`), not left by the killed process, which had used hundreds of tmp names.
  - `P1Copies::test_sigkill_with_copies_pending_rebuilds_them_on_restart`: fails 3 of 3 here (fsck of a packfile-URI reader: `unable to load rev-index for pack …`). **Cause not established.** My reconstructions of its reader path pass (`repro/r12_uri_fetch_fsck.py`, `repro/r12b_uri_fetch_after_local.py`), but they omit the hold/SIGKILL/rebuild steps. This could be a real post-crash copy-rebuild defect or an environment effect; it needs follow-up.
- Reference mode (`unix-fs`) and `tests/*.sh`: **not run**. Unix sockets are blocked here.

So F1–F11 are all invisible to the builders' passing suites.

## A1–A12 in the networked modes; builder claims contradicted by the code

- **A1** (stream the pack to the blob store without unpacking): holds for the local store. In S3 mode the whole pack is read into memory and PUT as one body (`blob.rs:171`, `s3.rs:310`), and GETs buffer whole objects (`s3.rs:319-326`), so memory per push or fill grows with pack size.
- **A2/A3**: unchanged. Each metadata read is now a network round trip (`meta.rs:774-806`); O(1) in count still holds.
- **A4**: deviation through the tip and pack keys in the read set (F13, executed).
- **A5/A7**: unchanged semantics (C2).
- **A8**: broken for off-host clients (F11).
- **A9**: covers are kept; duplicates become orphans (F10). Compaction can stop permanently after an outage (F1).
- **A10**: the metadata service is a single point of failure, which the builders state. Frontends depend on it with no timeout (F6).
- **A6, A11, A12**: not affected by the networked mode; not re-audited.

**Claims contradicted by the code:**
- `docs/DISTRIBUTED.md:33-34` says the sweep "never [removes] a live instance's in-flight files". F3 contradicts this (executed).
- `docs/NETWORK-STORAGE.md:36-41` says a downloaded object is verified and the pack's "size matches the index". `.alt` is not verified, and pack size is not compared (F9, read).
- `docs/DISTRIBUTED.md:54` (D8) says "maintain completes from either frontend" after restarts. It does not after an outage that hits the compactor (F1, executed).
- `docs/NETWORK-STORAGE.md:32-33` says a 412 is "verified". It is size-only for indexes and alt maps, and size plus trailer for packs (F2). Stated accurately, but insufficient to support "nothing is ever overwritten ⇒ store content is what was pushed".

## What could not be established

- **The builders' reference mode** (Unix-socket metadata plus shared directory): the sandbox forbids Unix-socket binds. TCP was used instead (same frontend logic).
- **Power loss, and real disk or `F_FULLFSYNC` behaviour:** not testable here. MinIO's own durability is not testable here either.
- **Multi-host behaviour** (NFS `flock`, network partitions, clock skew in SigV4): one host only.
- **F10 by execution:** making two frontends compute different alt maps needs duplicated objects as deltas in some packs and whole in others, plus a concurrent cover build. It was not attempted for budget reasons, so it rests on reading.
- **The `a`/`a/b` creation race across frontends** (documented by the builders): not executed.
- **Root causes of two builders' test failures:** `D2StalledCommit` (F12, intermittent in tcp-s3) and `P1Copies::test_sigkill_with_copies_pending…` (deterministic here). Both are open.
- **Single-process SIGKILL durability:** not re-run by me beyond the builders' `p0` (passed). My kill storms (C1) cover the networked modes only.
- **A6, A11, A12, shallow fetch, recompression, the simulator (`sim.rs`) and v0 protocol paths:** **not audited**.
