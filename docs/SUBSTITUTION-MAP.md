# Precomputed substitution map: registration (before measuring, 2026-11-07)

**Change:**
- At cover build time (background), record for each whole object in the cover the L0 pack and entry where the same object is stored as a delta. This goes in a write-once `<cover>.alt` sidecar, published before the cover's metadata transaction.
- The inline fetch path consults the map for cover packs instead of the global object index.
- Whether the delta's base is already emitted, or held by a thin client, is still decided at fetch time.
- Packs without a map (L0 packs, covers built before this change) use the object index as before.

**Accounting:**
- Map construction calls `locate` in the compaction thread, counted as **background** logical reads.
- Map consultations are counted separately (`alt_map_lookups`). They read a pack-local immutable sidecar, not the global index or metadata store, so they are not metadata reads. Reported alongside, never dropped.

**Measured with:** `verify/recompress/measure.py` (workload R, 300 versions), before = main 919aada, after = this branch. Registered expectations and checks:
1. **Equivalence (must hold):** inline clone bytes and every inline fetch response byte count are **identical** before and after (same substitutions chosen). URI results are unchanged.
2. **Foreground:** inline-fetch foreground metadata reads fall. Hypothesis: per-version object-index lookups at Δ = 256 drop from ≈522 towards the planner's ≈13, with `alt_map_lookups` taking their place.
3. **Total work reported:** background reads and CPU for push + compaction, `.alt` bytes stored, and process RSS after the run (memory).
4. **No regressions:** hidden CG1–CG4, frozen CG5/CG6, parallel download (4 jobs), P0, P1.

## Results (2026-11-07)
**Deviation from registration, criterion 1.** "Identical bytes before vs after" could not be tested as registered: the before and after runs build their repositories separately, and commit timestamps make every object differ, so sizes vary by a few bytes even between runs of one build. It was replaced by a stronger **same-data A/B** test (`verify/subst/equiv.py`): one data directory served by the same binary with maps ignored (`PANDO_ALT_MAP=0`) and with maps used.
- **All 11 responses are identical** (sha256): clone, plus Δ = 1, 4, 16, 64, 256, inline and URI.
- URI responses are compared with the cache-node port normalised, because those ports change on every restart.
- Raw data: `verify/results/subst-equiv.json`.

| Same data, inline fetch | Maps off: reads (index lookups) | Maps on: reads (index lookups) | Map consultations |
|---|---|---|---|
| clone | 1,011 (606) | 7 (0) | 606 |
| Δ = 4 | 15 (11) | 6 (2) | 9 |
| Δ = 16 | 45 (38) | 9 (2) | 36 |
| Δ = 64 | 147 (137) | 12 (2) | 135 |
| Δ = 256 | 537 (524) | 15 (2) | 522 |

Note: the first request in a process that needs the object index loads it lazily, about 400 keys, counted foreground. In the maps-off run the clone paid it (1,011). In the maps-on run the clone no longer needed the index, so the Δ = 1 request paid it instead (407). That is a one-time per-process load, moved in time, not removed.

**Total work** (workload R, separate runs, `subst-before-919aada.json` / `subst-after-*.json`):
| | Before | After |
|---|---|---|
| Background metadata reads (push + compaction) | 5,086 | 7,384 (+2,298, map construction) |
| Push + compaction CPU | 2.00 s | 2.06 s |
| `.alt` sidecar bytes stored | 0 | 6,894 |
| Server RSS after run | 162,464 KB | 159,984 KB (no increase) |

Conclusion: on workload R the inline path's foreground metadata work falls from linear in Δ to the planner's logarithmic cost. The per-object work now happens in background compaction, which is counted and reported, plus pack-local map consultations, counted separately. Responses are unchanged.
