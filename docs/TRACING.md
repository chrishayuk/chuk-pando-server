# Causal event timeline (REGISTERED before implementation, 2026-11-07)

**Purpose (owner):** follow four operations with real timestamps, ref versions, pack ids, metadata transactions and
causal relationships:
1. a push arriving at frontend A and committing its ref update;
2. frontend B discovering that update through the incremental change log;
3. background compaction preparing a reusable cover;
4. a fetch through B selecting packs and retrieving them.

## Design: read-only, offline
- **The server does no tracing work.** Every frontend already appends structured events (`events/events.jsonl`: node,
  per-node sequence, wall-clock ms, ids).
- **The timeline is built afterwards** by `verify/trace/timeline.py` from the event files of all frontends. Causal edges
  are joined on stable identifiers, never inferred from timing alone:
  | Edge | Join key |
  |---|---|
  | push received → pack ingested → pack stored → ref version committed → reported | push id (per node) |
  | ref version committed (A) → index refresh (B) | pack id + commit LSN in B's `discovered` list |
  | ref versions → cover committed | the cover's `members` (pack ids) and range (ref, lo, hi) |
  | pack → self-contained copy | `twin_committed.pack` |
  | cover / copy / pack → fetch | ids in `fetch_served.packs` and `uri_packs` |
  | fetch → bytes delivered by the URI cache node | pack id in the cache node's request (already counted in `/_stats`) |
- **The only server change:** the existing `object_index_built` event gains `discovered: [{pack, lsn}]` (at most 64
  entries, plus a count) and `hwm_from`. No new events, no new I/O, no new metadata reads.
- **Ordering across nodes** uses causality (LSNs, ids) first. Wall-clock ms are shown for reading but are only
  comparable on one host. This is stated in the output.

## Overhead check (registered)
Event bytes per push and per fetch before and after the change, on the unchanged tail harness, and the tail harness
rows (bytes, reads, writes) must be unchanged.

## Demonstration scenario (`verify/trace/demo.sh`, tcp-s3 deployment)
1. 4 pushes of a file through A; A's background compaction builds the L1 cover of versions 1–4.
2. A thin push of version 5 through B: B refreshes incrementally and discovers A's packs.
3. A packfile-URI clone through B: the plan selects the cover plus version 5's pack.

Output: `timeline.json` (events and edges) and a text rendering.

## Results (2026-11-07)
**Demonstration** (`verify/results/trace-demo/`: `timeline.txt`, `timeline.json`, raw `events-A.jsonl` and
`events-B.jsonl`; tcp-s3 deployment). Routing: version 1 through A, 2 through B, 3–4 through A, 5 through B.
- every push is followed from receipt to acknowledgment, with its pack id, single-pass validation, and the metadata
  transaction's LSN and ref version;
- A's background compaction builds the L1 cover (0,4]. The timeline links it to its four member packs, including the
  one pushed through B, and to its recompression attempt (kept spliced: not smaller);
- **B's incremental refresh** reads 7 change-log records from LSN 2 to 9 and discovers A's packs, copies and cover. Each
  discovery links to A's commit by pack id and LSN;
- B's clone plan: 2 steps, 4 metadata reads. It uses A's cover and version 5's pack, the latter served by URI through
  its self-contained copy.

The scenario routing was adjusted twice before this result. In the first runs B's index was first built at version 5,
so the refresh was a full build, not the incremental discovery the scenario is meant to show.

**Overhead check:** the unchanged tail harness (`tail-<commit>.json` vs `tail-33b7c66.json`). URI packs, metadata reads,
writes and copies are identical in all 12 rows; bytes delivered differ by ≤ 3 B, the same run-to-run noise as before.
The only added output is the bounded `discovered` list on index-refresh events, which are rare.

Event sizes in the demonstration: index-refresh events 217, 226 and 924 bytes (the last carrying 7 discovered packs), out of 12.5 KB of events in total.
