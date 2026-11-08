> **Historical.** The seed build's README, written by the autonomous builder and kept unchanged below for the record.
> Its gate claims are the builder's own; the independent results are in `verify/` and `docs/record/`. The current
> README is `../README.md`.

# pando-style Git server (Rust)

A Git smart-HTTP server whose storage is immutable packs in a blob store plus ref versions in a
transactional metadata store, following the brief. Entry point for the harness: `server/run.sh`.

| Document | Content |
|---|---|
| `INTERFACE.md` / `BRIEF.md` | the task |
| `MODEL.md` | precise protocol model, invariants, trace vocabulary, simulator (CG7) |
| `DECISIONS.md` | every design decision with alternatives and cost |
| `FEATURES.md` | Git feature support table (CG1) |
| `LAYOUT.md` | on-disk layout under `$G_DATA_DIR` |
| `LOG.md` | chronological work log incl. failures |

## Gate status (by my own tests, see `tests/`)

| Gate | Status | Evidence |
|---|---|---|
| CG1 stock git | PASS | `tests/cg1.sh` 23/23, `tests/features.sh` 36/36 |
| CG2 atomicity + crash safety | PASS | `tests/cg2.sh` 36/36 (races of every shape, lease, atomic, SIGKILL after ack / mid-push / kill cycles / mid-compaction), `pando sim` |
| CG3 immutable packs + deps | PASS | `tests/cg3.sh` 35/35 |
| CG4 concurrent branches | PASS | `tests/cg4.sh` with N=1000: 0 conflicts, RYOW, monotonic reads; `tests/stress.sh` (writers + readers with fsck) |
| CG5 explicit haves (ext.) | PASS | `tests/cg5.sh` 17/17 with the modified client built from `reference/openai-git` |
| CG6 compaction + parallel downloads (ext.) | PASS | `tests/cg6.sh` 20/20, parallel URI part of `tests/cg5.sh` |
| CG7 verification support | Provided | `MODEL.md`, `events/events.jsonl` + `tests/trace_check.py`, `pando sim --seed N` (deterministic, fault injection), `tests/model_check.py` (exhaustive, with mutants), `pando fuzz` |
| Metadata cost | measured | `tests/metrics.sh`: 4 reads / 4 writes per push for 1 commit, 1,000 commits or 20 MB |

Known limits: see `FEATURES.md` ("No" rows) and DECISIONS D7/D8/D9/D12.

## Running

```sh
export G_PORT=18080 G_DATA_DIR=/tmp/pando-data
server/run.sh start && server/run.sh create-repo demo
git clone http://127.0.0.1:18080/demo.git
curl -s http://127.0.0.1:18080/_stats
server/run.sh maintain; server/run.sh stop
```

Tests: `tests/run_all.sh` (all gates + 5 simulator seeds). CG5 needs the modified client:

```sh
mkdir -p $TMPDIR/ogit && rsync -a reference/openai-git/ $TMPDIR/ogit/src/ &&
  make -C $TMPDIR/ogit/src -j8 NO_GETTEXT=1 NO_TCLTK=1 NO_PERL=1 NO_PYTHON=1 all
```

Simulator: `server/target/release/pando sim --seed 7 --steps 1000 --crash 0.03 --fault 0.05`
(prints a JSON report, exit 1 on any invariant violation; trace in `<dir>/events/events.jsonl`).

## Code map (`server/src`)

`pack.rs` object/pack codec + sidecar index · `ingest.rs` single-pass streaming decoder ·
`meta.rs` metadata store (OCC, per-key commit locks, WAL group commit, checkpoints) · `blob.rs` blob store ·
`repo.rs` ref versions, push commit, fetch planner · `packout.rs` inline pack splicing ·
`proto.rs` receive-pack + upload-pack v2 · `v0.rs` upload-pack v0/v1 · `shallow.rs` depth fetch ·
`compact.rs` covers L1..L4 · `cache.rs` cache nodes + consistent hashing · `http.rs` hyper front end ·
`events.rs` trace log · `sim.rs` deterministic simulator · `main.rs` process management.
