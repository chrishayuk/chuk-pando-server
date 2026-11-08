# Cover compression vs Git repack: workloads and decision rule (REGISTERED before running, 2026-11-07)

Purpose: decide whether to implement recompressed covers (docs/RECOMPRESSION-ACCEPTANCE.md). Workload R (`docs/RECOMPRESSION-BASELINE.md`) is preserved unchanged and not re-run here.

## Workloads (`verify/recompress/workloads.py`, seed 11; 260 versions each, pushed one commit per push by stock git 2.55.0)
| Id | Content | What it probes |
|---|---|---|
| **S** (cross-file similarity) | 120 files, each derived from one of 6 shared 16 KiB templates with ~5 % per-file line edits; each version edits 1–3 random lines in 1–4 random files | deltas *across paths*, which splicing cannot create but a repack can |
| **C** (copies and moves) | 60 files of 8 KiB; each version either edits a file, or copies an existing file to a new path with a few edits (about 1 in 3 versions), or renames one | new paths whose best base is another path's blob |
| **U** (control: unrelated) | 60 files of 8 KiB of independent random text; each version edits 1–4 random lines in 1–3 files | no cross-file similarity: repack should gain little |

## Measurements, per workload
For the **L4 cover of versions 1–256** (self-contained, from the empty repository), and for **one L2 cover (versions 17–32)**:
- **Completeness:** the cover pack is indexed alone (`git index-pack --strict` in a scratch repository). Its object set must equal the objects introduced by those versions:
  - L4: `git rev-list --objects v256`;
  - L2: `git rev-list --objects v32 ^v16`.

  Any mismatch is reported and invalidates the size comparison for that cover.
- **Sizes (bytes):**
  - the B cover pack;
  - Git "conventional": `pack-objects --threads=1`, default window 10 / depth 50;
  - Git "aggressive": `--window=250 --depth=4095`.

  Both Git packs are built from the same object set, self-contained, `--delta-base-offset`, from a fully repacked source repository, so that deltas are actually computed. The R-baseline mistake (piping loose objects) is avoided, and each Git pack is checked with `index-pack`.
- **The sum of L0 push-pack bytes for the same versions,** for context.

## Decision rule (fixed now)
**Implement recompression** if, on **S or C**, the aggressive Git pack is **at least 10 % smaller** than B's L4 cover, **or** the conventional Git pack is at least 10 % smaller than B's L2 cover, with completeness verified for all three packs.

Otherwise, record recompression as unnecessary for the tested workloads.

On **U** a large Git advantage would indicate a measurement fault, not a reason to recompress; it is reported either way.

## Result (run 2026-11-07, `verify/results/workloads-SCU.json`; harness at this commit)
- **Method self-check:** workload R v256 aggressive pack = 195,263 B, against 195,106 B from `git repack`.
- **Completeness:** for every cover and every Git pack, the object set equals the required set (no missing, no extra). Strict indexing applied to the covers that start from version 0.

| Workload | Cover | B spliced | Git conventional | Git aggressive | Rule |
|---|---|---|---|---|---|
| S | L4 1–256 | 781,139 | 1,122,556 | 389,778 | aggressive −50.1 % → **met** |
| S | L2 17–32 | 392,211 | 193,277 | 110,594 | conventional −50.7 % → **met** |
| C | L4 1–256 | 872,080 | 826,430 | 420,907 | aggressive −51.7 % → **met** |
| C | L2 17–32 | 79,432 | 73,829 | 73,800 | conventional −7.1 % |
| U | L4 1–256 | 459,052 | 460,026 | 453,994 | −1.1 % (control: no cross-file gain, as expected) |
| U | L2 17–32 | 153,549 | 151,582 | 151,582 | −1.3 % |

**Decision: implement recompression** (the rule is met on S and C).
- Splicing preserves each path's own delta chain (workload R) but cannot create deltas *across* paths, which Git's window search finds.
- Note that Git's conventional depth-50 pack is *larger* than B's spliced L4 cover on S: depth limits cost bytes. Recompression parameters must therefore be chosen and measured, not assumed.

Two harness faults were found and fixed before this run; neither is a server defect:
- strict indexing had been applied to range covers, which legitimately reference earlier versions;
- a detached `gc --auto` raced the harness's `repack`; auto-gc and maintenance are now disabled for the harness's Git calls.
