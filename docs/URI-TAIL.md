# URI tail delivery: registration (before measuring, 2026-11-07)

**Facts established from source:** both stock Git (`fetch-pack.c:1930`) and the OpenAI fork (`fetch-pack.c:1798, 2176`) verify a packfile-URI pack's hash **after** `index-pack --fix-thin`. A thin pack served by URI therefore fails. Every URI-served pack must be self-contained, in any Pando-like design.

**Consequence:** a reader that needs only the newest version of a large, edited file must receive at least one full copy of it by URI. The design question is **who pays for that copy, and how often**:
- **(a) Stock-client pushes (thin):** the push pack is small. The server builds a self-contained "thick twin" on first URI fetch, a full copy, once per tail version, reused by later readers.
- **(b) OpenAI-fork pushes with explicit haves:** the server asks for `no-ref-delta`, so the client pushes a self-contained pack (full copy at push time), served verbatim with no twin.

**Measurement** (`verify/tail/measure.py`, workload R reduced to 40 versions, then `maintain`; reader states Δ = 1, 2, 3, 4, 8, 16 behind the tip; one stock reader clone per state, with `fetch.uriprotocols=http`). Recorded per pusher (stock / fork):
- sum of L0 push-pack bytes and push wall time;
- per reader state:
  - bytes delivered by cache endpoints (`/_stats bytes_out_cache` delta);
  - URI pack count;
  - twins built (`twin_committed` events), and their bytes;
  - foreground metadata reads and writes during the fetch (fetch-path writes = twin publication).

**Not a pass/fail test.** It establishes where the cost lands before any tail optimisation is designed. Options to be evaluated afterwards against the same harness:
- build twins in background compaction instead of on the fetch path (B D-13);
- group the uncovered tail into one shared pack;
- a hybrid inline tail (deviates from the article's "exclusively", A8).

**Harness fix before the counted run (2026-11-07):** the first run's readers were cloned from the writer's repository, so they already held every object and every fetch moved 0 bytes. That run is discarded except for its push-side numbers. Readers now start empty, receive exactly the history up to their state, and the harness asserts they lack the tip beforehand and reach it afterwards.

## Results (`verify/results/tail-712348e.json`; workload R reduced to 40 versions, main 712348e)
| Reader behind | Stock pushes: URI packs / bytes delivered | Copies built during fetch | Fork pushes: URI packs / bytes delivered |
|---|---|---|---|
| 1 | 1 / 129,534 | 1 (129 KB; 2 metadata writes on the fetch path) | 1 / 129,064 (none) |
| 2 | 2 / 259,049 | 1 | 2 / 258,103 |
| 3 | 3 / 388,539 | 1 | 3 / 387,117 |
| 4 | 1 (L1 cover) / 130,267 | 0 | 1 / 129,791 |
| 8 | 2 / 260,420 | 0 | 2 / 259,465 |
| 16 | 4 / 520,401 | 0 | 4 / 518,436 |

Push side, 40 versions: stock 139,860 B in total (thin, about 3.5 KB per push); fork with explicit haves 5,137,546 B (self-contained, about 128 KB per push, ≈37×).

## Reading
1. **Reader bytes are the same whichever client pushed.** On this single-large-file workload every URI-served pack carries one full copy of the file, so a reader pays roughly *(number of URI packs) × (one full copy)*.
2. **The pusher only decides who makes the copy:**
   - the fork uploads it on every push (≈37× upload bytes here), and the server reuses the push pack verbatim;
   - stock pushes stay small, and the server builds one self-contained copy per tail version, on the first fetch that needs it (currently *on the fetch path*: CG7 audit D-13). Later readers reuse it.

   Both paths store one copy per version. There is no reader-count crossover in bytes delivered; the trade is upload bandwidth (fork) against server CPU and storage done once per version (stock).
3. **The real reader cost is pack count.** A reader 2–3 versions behind receives 2–3 full copies; one 16 behind receives 4 (L1 alignment at tip 40). Grouping the uncovered tail would save at most 2 copies for readers 2–3 behind. It would need a non-aligned "tail cover" rebuilt on every push.
4. This is a worst case: a 256 KiB file rewritten every version. With many small files, a copy per pack costs only the files changed in that pack's versions. The single-file workload overstates the effect for typical source trees.

## Decision (proposed)
- Do **not** build tail grouping now: the gain is bounded (at most 2 copies, readers 2–3 behind) and it adds a non-aligned, per-push cover type.
- **Do** move self-contained copy construction for thin pushes from the fetch path to background work at push time (D-13). Fetch then becomes read-only, matching Pando's design of moving work to the background, as the article describes it (paraphrase; https://openai.com/index/scaling-git-at-openai/). Measured with this same harness: fetch-path writes should go to 0 and copies built during fetch to 0, with bytes delivered unchanged.
