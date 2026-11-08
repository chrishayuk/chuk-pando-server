# Recompressed covers: acceptance criteria (FROZEN before implementation, 2026-11-06)

Fixed by the owner before any recompression code was written. The baseline is measured on `main` at 09cab06 with the harness named below, before the change. The same harness and workloads are re-run after.

## Scope
- Only the **content** of cover packs changes: from de-duplicated splices of member L0 packs to recompressed packs (objects re-delta'd).
- Pack selection (planner, cover levels, cover ranges, the `linked` rule) is NOT changed in the same step, so that effects can be attributed to recompression.
- Background compaction may decode and recompress objects. It may use Git plumbing in an isolated, temporary, throw-away workspace. That workspace must never become authoritative storage. The push path is unchanged: packs are stored and validated as received, never unpacked.

## Criteria
1. **Push packs unchanged.** Original pushed (L0) packs remain byte-identical and read-only: CG6 `test_cg6_pushed_packs_byte_identical_after_maintain` and P0 `test_published_packs_are_readonly_and_never_replaced` pass.
2. **Covers immutable and valid.** Each new cover pack is published write-once and is independently valid: it passes `git index-pack --strict` on its own, is self-contained, and contains no REF_DELTA, so it can be served with `no-ref-delta`.
3. **Bytes reduced.** Recompression measurably reduces stored and served bytes on a repeated-content workload. Workload R: one ~256 KiB text file with a small edit per version, 300 versions. Reported as total cover bytes per level and URI-clone bytes, before vs after. There is no pass threshold beyond "measurably smaller"; the numbers are reported as measured.
4. **Clients.** Stock Git (2.55.0 and Apple 2.54) and the OpenAI fork can clone and fetch (inline and packfile URIs, and the fork with `fetch.packfileUriJobs=4`), and `git fsck --full` is clean. The hidden CG1–CG4 suites, the frozen CG5/CG6 suite and the P0/P1 tests produce no new failures relative to the baseline.
5. **Crash and concurrency safety.** Cover publication survives crashes, concurrent pushes and `maintain`: CG6 `sigkill_during_maintain`, `maintain_concurrent_with_pushes` and `every_ref_version_complete_after_maintain` pass. Any temporary workspace left by a crash is cleaned up and never served.
6. **Costs re-measured honestly.** Inline-fetch and URI-fetch metadata reads (honest counters, docs/METRICS.md), plus server CPU, are re-measured with the same harness. Nothing is excluded from the counts. The hypothesis that inline per-object lookups fall is tested, not assumed. Compaction CPU and time are reported too.

## Measurement harness
- `verify/recompress/measure.py`: workload R, plus the planning chain from docs/PLANNING.md.
- Results: `verify/results/recompress-{baseline,after}-<commit>.json`.
