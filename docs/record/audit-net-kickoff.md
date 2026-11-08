You are independently auditing one implementation of a published server architecture ("Pando", a Git-compatible backend). Its builders believe it is correct. Your job is to find where it is not: by reading the code, and above all by **executable counterexamples**. You will not see earlier audits, test results or the builders' notes, and you must not look for them. The builders' own documents (`impl/docs/`, `impl/*.md`, code comments) are **claims to check, not evidence**. Some code comments mention earlier defect ids; treat them as claims too.

## What you have
- `inputs/source/article-extract.md` and `article-extract-addendum-1.md`: what the published article says.
- `inputs/spec/01-claims-ledger.md`: the architectural mechanisms A1–A12.
- `inputs/campaign/INTERFACE.md`: how a test harness drives the server (`server/run.sh start|stop|create-repo|maintain`, `G_PORT`, `G_DATA_DIR`).
- `impl/`: the implementation, an exact snapshot of one commit (results directory removed).
  - The Rust server is `impl/server/src/`.
  - Deployment modes:
    - a single process;
    - several frontends with a metadata service (`pando meta-serve`, Unix socket or `tcp:`) and either a shared pack directory (`PANDO_BLOBS`) or an S3-compatible store (`PANDO_S3`, `PANDO_S3_KEY`, `PANDO_S3_SECRET`).
  - The builders' tests are in `impl/verify/` (`p0`, `p1`, `dist`, `net`, …), and `impl/verify/dist/distlib.py` starts multi-frontend clusters including MinIO.
- `clients/`:
  - stock Git 2.55.0 (`clients/git-stock`);
  - the harness library the tests import (`clients/heldout/harness/lib.py`). The `verify/*/run.sh` scripts find it with
    `CHUK_PANDO=$PWD/clients`. Also set `G_CLIENTS=stock=$PWD/clients/git-stock`, and `SERVER_DIR` or `G_RUN_SH` to
    your copy;
  - MinIO (`clients/minio`; set `G_MINIO` to it).
- **Building:**
  - `impl/` is read-only, so copy it to `work/` and build there: `CARGO_HOME=$PWD/impl/.cargo-home CARGO_NET_OFFLINE=true cargo build --release`.
  - There is no network beyond localhost.
  - Write everything under `work/`.

## Focus areas (in priority order). For each: what the code does, where it can fail, and a reproducing test if it can

1. **Acknowledged-write durability.**
   - Can any push be acknowledged, to a Git client or through the metadata service, before its pack, index and metadata transaction are durable?
   - Trace the exact order of writes, fsyncs, renames/links, uploads and the commit reply, relative to the acknowledgement, in every deployment mode.
   - Look for windows that lose an acknowledged ref or leave a ref pointing at an object no frontend can serve after SIGKILL, restart, or loss of the metadata service or object store.
   - Note what is assumed rather than enforced, e.g. the object store's own durability.
2. **S3 publication semantics.**
   - Conditional create (`If-None-Match: *`), retries, timeouts, partial uploads, lost replies.
   - Two frontends publishing the same object.
   - A 412 for content that differs.
   - The order of the pack upload and the index upload.
   - Error responses the client might misread.
   - Can anything overwrite a published immutable object, or can a ref be published whose objects are absent from the store?
3. **Cache correctness.**
   - Each frontend's local cache in S3 mode.
   - Can a crash, restart, concurrent fill or partial write leave a corrupt or wrong cache entry that is then served or treated as authoritative?
   - Is every downloaded object verified, and how?
   - Shared-directory mode: the temporary-area sweep and its locks.
4. **Distributed consistency.**
   - Per-ref compare-and-swap across frontends.
   - The metadata service's optimistic transactions.
   - The change log and "stable LSN" used for incremental index refresh: can out-of-order application, retries, checkpoints, restarts of the service or a frontend, or concurrent maintenance cause:
     - a missed pack;
     - a lost acknowledged ref;
     - a refused valid push ("missing objects" for committed objects);
     - stale visibility?
   - Commits whose reply is lost.
   - Duplicate background work (covers, self-contained copies) from two frontends.
5. **Test coverage.**
   - Produce an **inventory**: every test file and test function (unit tests in `server/src`, `verify/*`), what property each actually checks, and whether it can fail. Look for tests that are vacuous, skip silently, or pass for the wrong reason.
   - Map focus areas 1–4 to tests, and list the important properties with **no** test.
   - Check whether anything guards against tests being silently deleted or not run.

Also, briefly: A1–A12 fidelity (spec/01) where the networked modes change it, and any material builder claim the code contradicts.

## Rules for evidence
- Prefer **execution**. For each suspected defect, write a minimal reproducing script or test under `work/repro/` that fails on this snapshot, and run it. Fault injection is encouraged: kill processes at chosen points, stop MinIO, corrupt objects, delay requests (e.g. a small proxy in Python), instrument a copy of the code.
- Cite `file:line` for every code claim. Say for each finding whether it was **executed** or **read only**.
- Do not modify `impl/`. Do not fix anything: report.

## Output
Write `work/AUDIT.md`:
- **summary table** (finding → severity blocker/major/minor → executed/read → reproducer path);
- **answers per focus area**;
- **test-coverage inventory and gaps**;
- **what you could not establish**, and why.

Keep it updated as you go, so a budget cut-off leaves a usable record. Mark anything not yet examined as **not audited**. Do not narrow the scope silently.
