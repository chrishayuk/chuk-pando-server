# Roadmap after 0.1.0

0.1.0 is released and frozen: its tag, release assets and verified snapshot are not changed. Everything below
comes after it. Each item is either a later experiment, a documentation task, or a limitation kept on purpose.
Source: a review of this implementation against OpenAI's article (2026-11-08).

## Next experiment: commit-to-version mapping
**The question:** how much extra indexing work at push time is worth accepting to make later reads cheaper?

**Where we are now:**
- The fetch planner recognises a client's `have` only when it is the tip of a recorded ref version (DECISIONS.md D7).
- A commit partway through a push pack is not recognised. A client whose newest common commit is such a commit gets
  more history than it needs, and possibly more packs.
- The article's planner looks haves and wants up through commit mappings recorded at push time. Its split between
  synchronous and asynchronous writes for very large pushes exists to keep that mapping cheap. This design records
  no per-commit metadata, which is why docs/LARGE-PUSH.md found the split unnecessary. Both differences come from
  this one choice.

**Possible benefit:**
- fewer bytes and packs for incremental fetches from intermediate commits: CI checkouts of a mid-push commit, and
  readers that fetched between pushes from other clients;
- packs are reused better when readers hold different intermediate states, which is what makes trading data
  efficiency for parallelism worth it.

**Costs to measure:**
- metadata written per pushed commit (keys, bytes), against today's constant 3 reads and 4 writes per ref;
- extra work on the push path before acknowledgment, and whatever can move to background work;
- index rebuild and memory after a restart;
- planner reads per have: a have still has to cost a bounded number of reads.

**Correctness requirements:**
- A have may only be treated as proof that the client holds a version's closure if the commit is really in that
  version's history. Ranges after a force-push or delete must not be trusted (the same `linked` rule as covers).
- Clients with shallow history keep today's narrower rule (D7).
- A push is never acknowledged before the mapping it depends on is durable, or the planner must work correctly
  without it (falling back to tip-only).
- Recovery rebuilds any mapping work that was not finished. Background work is idempotent.
- Stock Git and OpenAI's client both keep working. CG1–CG6, P0, P1 and the distributed tests show no new failures.

**Benchmark** (register it before measuring, as with docs/LARGE-PUSH.md):
- the tip-only planner against a planner that knows commits, on identical incremental-fetch workloads: clients
  holding commits at random points inside pushes, at several distances Δ, on linear and branchy histories;
- report metadata reads (logical, honest counters), server CPU, pack count, pack reuse and total bytes sent, for
  inline and packfile-URI fetches, plus the push-side costs listed above;
- set a decision rule in advance (for example, adopt only if bytes sent fall by a stated amount at a stated cost to
  pushes).

No implementation before this is registered and measured. It could be the subject of a second video.

## Documentation
- **Planning cost wording (docs/PLANNING.md):** state the mechanism alongside the measurements:
  - the largest cover (L4) spans 4⁴ = 256 versions;
  - above Δ = 256, each further 256 versions adds one L4 cover read (steps 10 → 13 → 17 → 25 at Δ = 256, 1,024,
    2,048 and 4,096);
  - up to Δ = 1,024 that linear term (+3) equals the logarithmic trend, so the measured curve first visibly departs
    between 1,024 and 2,048.

  So "logarithmic up to ~1,024 versions, then +1 read per 256 versions" describes the measurements correctly. The
  mechanism is linear in Δ/256 from Δ = 256.
- **docs/record/CG7-SUMMARY-AND-GAPS.md:** mark it as a historical snapshot (2026-11-06), not the current assessment.
  Several of its grades (A3, A9, A10) and gap items have since been resolved.
- **DECISIONS.md D15:** its reasoning still talks about a "stripe lock". Re-state it for exact per-key locks.

## Kept as declared limitations
| Limitation | Why it stays |
|---|---|
| Stock Git is shown every ref on push; only clients sending explicit haves get trunk alone (D2) | Keeps update, delete and `--force-with-lease` exactly as Git does them for stock clients. The cost (packs depend on more refs, the advertisement grows with the number of refs) is shown, not hidden. |
| Inline packs are the default for stock Git; packfile URIs need `fetch.uriprotocols` | Stock Git asks for URIs only when told to. |
| Planning is logarithmic only up to the L4 cover span (see above) | Matches the article's "up to L4". Going higher would mean more levels: possible later, not planned. |
| One unreplicated metadata service: a single point of failure (docs/DISTRIBUTED.md) | Replication is out of scope for a reproduction. |
| No regional caches, no separate metadata-cache service, no read-through proxy for migrating | Operational scale, not architecture. |
| Cover recompression runs `git pack-objects` in a throw-away workspace | Background work only. Push packs are never unpacked or rewritten. |
