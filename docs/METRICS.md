# Metadata metrics (`/_stats`)

`metadata_reads` / `metadata_writes` are **foreground logical** counts: work done while serving requests.

A logical read is any read of a metadata record, however it is served:
- a point read from the store;
- each key returned by a range scan (an empty scan counts 1);
- a hit in an in-process cache of metadata records (ref versions, covers, twins);
- a lookup in the in-memory object → pack index.

In a distributed deployment each of these would be a metadata-service read. Counting only physical store operations (the seed's behaviour, still exposed as `store_read_ops`) under-reported planner work. See CG7 audit B D-1 in the experiment record.

| Field | Meaning |
|---|---|
| `metadata_reads`, `metadata_writes` | foreground logical reads / written keys |
| `metadata_reads_background`, `metadata_writes_background` | the same, for compaction, checkpointing, `maintain` and startup catch-up (thread-local attribution) |
| `metadata_read_breakdown.{store_point_reads,store_scan_keys,cache_hits,object_index_lookups}.{foreground,background}` | the categories that sum to the totals |
| `plan_steps` | fetch-planner steps (foreground/background) |
| `store_read_ops`, `store_write_ops` | physical store operations (the old counters) |

**Per fetch:** the `fetch_served` event carries `plan_metadata_reads`, the exact logical reads of that one fetch plan. It is counted thread-locally, because planning does not yield, so concurrent requests do not mix.

**Measurement:** `verify/planning/run.sh OUT.json V deltas...` builds a V-version fast-forward chain, runs `maintain`, and records per Δ, for inline and packfile-URI fetches:
- the plan's reads;
- the whole request's foreground reads and their breakdown;
- steps, covers used and packs selected.
