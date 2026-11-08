# Claims ledger: "Scaling Git at OpenAI" (OpenAI, 2 Nov 2026)

Source: https://openai.com/index/scaling-git-at-openai/ (authors: Aaron Friel, Taylor Blau, Ted Nyman).
Every row is tagged with its evidence class. Only class **A** claims can be tested by us.

- **A** architectural mechanism: testable on our own build
- **P** production number: OpenAI's workload and old backend, not reproducible; reference target only
- **V** verification / process claim: assessed by what artifacts are published
- **S** synthetic: the article's own simulation, not a measurement

## Production numbers (class P: do not claim to "reproduce")
| ID | Claim (article wording) |
|----|-------------------------|
| P1 | Median push latency cut "by nearly 5x" (later: "nearly 80%") |
| P2 | "nearly a petabyte of Git data per day", "with just a handful of CPU cores" |
| P3 | 57 TB served in a single hour, >99% from local disk cache |
| P4 | Express pack cache hit rate >99% (consistent hashing to route pack requests) |
| P5 | Clone/fetch saturates 10 Gbps links on standard cloud VMs |
| P6 | Merge reliability: previously "at times falling below 90%"; afterwards the article says only that switching to Pando "improved merge reliability" (no figure) |
| P7 | Median push pack <20 KiB, p99 <3 MiB (trunk-only advertisement) |
| P8 | Kickoff to production in 10 weeks; serving traffic since April 2026 |
| P9 | Monorepo commit growth +2,442% vs Q1 2024 (Q4 2026 *estimate*) |

## Architectural mechanisms (class A: our targets)
| ID | Mechanism |
|----|-----------|
| A1 | Push streams pack to blob store, validated by a single-pass decode, **without unpacking into a local repo** |
| A2 | Push records a *ref version* {new tip, source pack, dependency ref versions} in a metadata store via a multi-key transaction that also moves the ref pointer (CAS-like). Gives read-your-own-write |
| A3 | Push metadata cost is **O(1)** reads/writes regardless of push size (large pushes write only a subset of commits synchronously; rest async) |
| A4 | Only contended key is the per-ref pointer. Pushes to unrelated refs never contend. **Per-ref serializability** |
| A5 | **Causal consistency** across refs: a reader of a ref observes the writer's history; no repo-wide snapshot, no ordering of ref updates within a clone/fetch (foo@1,bar@2 vs foo@2,bar@1 both legal) |
| A6 | Push advertises only a **designated trunk** ref (not all refs). Client packs against trunk alone, so the pack is reusable by any reader who has trunk. Topic pushes re-include earlier topic commits |
| A7 | Fetch maps haves/wants to ref versions, then selects stored packs. **O(log Δ)** metadata reads, Δ = shortest distance in ref versions between have and want |
| A8 | Fetch response is an **empty inline pack plus `packfile-uris` only**; client downloads packs from a cache service ("Express") |
| A9 | Compaction: each push = L0 pack; background workers build geometric **covers** over ref-version ranges (L0 to L4); covers act as a skip list; **packs at every level are kept** (no GC) |
| A10 | Servers only negotiate and select packs, so they run on commodity hardware; add servers / metadata units / blob stores to scale writes |
| A11 | "Explicit haves" (client change): server advertises a branch tip separately from history the client may omit; enables `--force-with-lease` + trunk-only pack |
| A12 | Full compatibility with unmodified server-side Git protocol *for the workflow*; **note** OpenAI also ships a modified Git client build (parallel pack download/index, explicit haves) |

## Process / verification claims (class V)
| ID | Claim | Published evidence |
|----|-------|--------------------|
| V1 | Core designed by one engineer, implemented by teams of agents | none beyond statement |
| V2 | Rust, greenfield, conformance suite vs standard Git behaviour | suite not published |
| V3 | Deterministic simulation testing found race conditions in negotiation and pack planning | harness not published |
| V4 | GPT-6 Astra wrote Lean and TLA+ "theory of Pando", used to find bugs and "prove properties" | **no models, properties or proofs published**. Unknown what was proved vs model-checked |
| V5 | Hundreds-of-hours agent sessions; round-the-clock red-teaming | none |

## Synthetic simulation (class S)
1,000 clients, 3PC vs object-store WAL vs Pando, "Synthetic teaching models, not production benchmarks." Model parameters are not given in the article text. Do not cite as evidence for or against.

## Things the earlier chat summary got wrong or missed
1. **Client is not fully stock.** OpenAI ships a modified Git (explicit haves, parallel pack download). Our test must say which behaviours work with unmodified Git 2.x and which need client changes. Risk to Gate 1.
2. Fetch is **packfile-uri only**: needs protocol v2 with `packfile-uris` and client `fetch.uriprotocols`. Mechanism A8 is the first feasibility question.
3. Trunk-only advertisement has a **stated cost** (stacked branches resend everything outside trunk). Include in the benchmark; it's a trade-off to measure, not hide.
4. The article reports reliability and latency but **no baseline hardware or workload**, so the "5x" has no reproducible comparator.
5. The earlier "Test 2–5" were placeholders; real ones are in `02-falsifiable-tests.md`.

## Not disclosed (builders must decide; log each as an assumption)
Metadata store technology; ref-version schema; how "dependencies" are proven to a reader; exact cover/compaction policy and sizes; deletes, force-pushes, tags, unreachable-object handling; shallow/partial clones; auth; how multiple trunks or reads of non-advertised refs work; behaviour when a reader's haves are unrelated to any ref version.
