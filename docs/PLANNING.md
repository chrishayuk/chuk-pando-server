# Fetch-planning cost: measured (2026-11-06)

**Method:** `verify/planning/run.sh` on commit 6e4b06a (honest logical counters, docs/METRICS.md).
- Workload: one fast-forward chain of 4,200 versions on `main`, then `maintain` (covers L1–L4, fanout 4).
- Each fetch: want = tip, have = the commit Δ versions behind.
- 3 repetitions per point, all identical.
- Raw data: `verify/results/plan-V4200-p1.json`.

| Δ | 1 | 4 | 16 | 64 | 256 | 1,024 | 2,048 | 4,096 |
|---|---|---|---|---|---|---|---|---|
| Planner logical reads | 4 | 4 | 7 | 10 | 13 | 16 | 20 | 28 |
| Planner steps (= covers used, from Δ=4) | 1 | 1 | 4 | 7 | 10 | 13 | 17 | 25 |
| Whole request, packfile URIs | 4 | 4 | 7 | 10 | 13 | 16 | 20 | 28 |
| Whole request, inline pack | 7 | 16 | 55 | 202 | 781 | 3,088 | 6,164 | 12,316 |

## What this supports
- **Up to Δ ≈ 1,024, planning is logarithmic:** about 3 extra logical reads per 4× increase in Δ (≈ 1 + 3·log₄Δ).
- **Beyond that it is linear, with a small constant:** one extra read per 256 versions. Covers stop at L4 (4⁴ = 256 versions), as in OpenAI's monorepo, which the article describes as using covers up to L4 (paraphrase). So the honest statement is *O(log Δ) up to the top cover span, then O(Δ/256)*. Unbounded O(log Δ) would need higher levels (an optional follow-on).
- **With packfile URIs, Pando's serving mode, the request's metadata work equals the plan's.**
- **The inline fallback** (clients that do not request URIs) does about 3 object-index lookups per version while splicing the pack. That is per-object work for building an inline pack, not planning.

## Caveats
- One linear history only. Branchy histories, stacked topics and force-pushes were not measured here; see the CG6 suite for correctness on those.
- A single server process with in-memory caches. Counts are logical (cache hits included), so they would carry over to a networked metadata store, but latency would not.

## Effect on the frozen CG6 planning test (run 20261106T233524Z, commit 184c2a4)
The frozen blind test measures `/_stats metadata_reads` around a whole fetch by a stock client **without** packfile URIs. Its pre-registered reads rule now **fails**: R = 46 / 76 / 178 / 555 at Δ = 4 / 16 / 64 / 256. Its URI pack-count half still passes (1 / 4 / 7 / 10).

On the seed, with the old counter, the same test passed. The difference is the honest counter: the work was always there.

**Cause:** inline pack splicing (`packout::write_spliced_alt`) looks up every whole object taken from a cover (`alt` → `Core::locate`), to substitute the smaller delta stored in its original L0 pack. Covers are de-duplicated but not recompressed, so they hold whole copies. That is per-object work: linear in the objects sent, not in planning.

The planner itself, and URI fetches, remain logarithmic (table above). The real fix is recompressed covers (gap list item 9), which would make the substitution unnecessary. Re-labelling the lookups as non-metadata would hide real work and is not an option.
