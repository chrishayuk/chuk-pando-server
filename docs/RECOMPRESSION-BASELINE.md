# Recompression baseline: findings before implementation (2026-11-07)

**Harness:** `verify/recompress/measure.py`, workload R: one 260,865-byte text file, one line edited per version, 300 versions pushed by stock git. Server `main` at 37b5e22. Raw data: `verify/results/recompress-baseline-37b5e22.json`.

## Measured
| Quantity | Value |
|---|---|
| L4 cover (versions 1–256, 768 objects) | 195,270 B |
| L3 / L2 / L1 cover (one each shown) | 144,251 / 131,551 / 128,387 B |
| Same 256 versions, `git repack -adf --window=250 --depth=4095` | 195,106 B (within 0.1 % of the L4 cover) |
| Inline clone (stock git) | 207,331 B out; 640 foreground metadata reads (615 object-index lookups) |
| Packfile-URI clone (stock git) | 2,502,059 B out (2,416,005 from cache endpoints); 43 reads |
| One thin push pack vs its "thick twin" served by URI | about 311 B vs about 128,300 B |

## What this shows
1. **B's covers already keep near-optimal deltas on this workload.** Splicing consecutive L0 packs preserves their deltas, and each cover holds one full copy per delta chain. A Git repack of the same range is no smaller. Recompressing covers would not measurably reduce bytes here, so criterion 3 is not expected to show a gain on workload R. A workload with cross-file similarity (many files, shared content) is needed to test whether real re-delta'ing helps.
2. **The URI path's byte cost comes from self-contained packs, not compression.** Stock Git cannot index a thin pack received through a packfile URI. So every URI-served pack carries a full copy of each chain it touches:
   - each cover costs about one full file;
   - each not-yet-covered tail version is served as a thick twin of about one full file.

   A URI clone of 300 versions therefore moved about 12× the bytes of an inline clone. The article (https://openai.com/index/scaling-git-at-openai/) accepts, in our paraphrase, that a client may download some objects it already has; this measures how much.
3. **The inline path's per-object lookups are mostly commits and trees.** At Δ = 256 there were 522 lookups, about 2 per version. Git rarely deltifies commits, and small trees are rarely deltified either, so recompressing covers would not remove these lookups. The lookups come from `alt` substitution, which checks every whole object taken from a cover.

## Implication
The hypothesis behind prioritising recompression (fewer bytes, fewer inline lookups) is not supported on workload R, and point 3 argues against it in general. Before implementing, the owner should decide between the options in the session report:
- recompression as specified, run against a multi-file workload;
- precomputing the substitution map at cover-build time (moves the per-object lookups to background work);
- reducing URI-path bytes (fewer thick twins / tail handling).

## CORRECTION (2026-11-07): the "≈12×" URI-clone figure was inflated by a planner regression
The baseline above was measured on 37b5e22. That build carried a regression from the first D-9 fix: covers starting at version 0 counted as unlinked (version 1 is a branch creation), so a clone from empty skipped the top covers. It downloaded many small covers and thick twins instead.

With the fix (`linked_inner`, branch p1b), the same workload-R URI clone moves **868,899 B against 207,334 B inline (≈4.2×)**, with 9 foreground reads instead of 43 (`verify/results/recompress-after-c2dd2f8.json`).
- The remaining overhead is still the self-contained URI packs: one full copy per chain per served pack, plus thick twins for uncovered tail versions.
- The **≈12× figure must not be used.** The ≈4.2× figure applies to workload R on this prototype only.

Recompression itself changed nothing on workload R, as predicted:
- cover bytes within ±11 B;
- inline lookups 615 → 606;
- background CPU for push + compaction rose from 1.31 s to 2.24 s.
