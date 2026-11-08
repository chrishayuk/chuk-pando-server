# What compaction does with objects (implementation boundary)

**Push path (unchanged, matches the article's A1):** an incoming pack is streamed to blob storage, validated by a single-pass decode, and published write-once. It is never unpacked into a Git repository; no `git` process is involved.

**Background compaction (this implementation's choice):**
- Cover packs are first built by splicing member push packs (no `git`).
- They are then recompressed with Git plumbing (`init`, `index-pack`, `cat-file --batch`, `pack-objects`) in a **temporary scratch repository** under `<data>/blobs/tmp/`.
- That repository is:
  - never authoritative storage;
  - never read by the fetch path or served;
  - deleted when recompression finishes;
  - swept on startup, only while the process holds the exclusive data-directory lock.
- The recompressed pack is accepted only after our own decoder verifies the same object set, no REF_DELTA, and a smaller size. It is then published write-once and synced **before** the metadata transaction that makes the cover visible.

**Attribution:** OpenAI's article (https://openai.com/index/scaling-git-at-openai/) says only, in our paraphrase, that a compaction worker claims a cover and produces a single packfile for it, and that this improves the compression ratio by reorganising the objects inside that packfile. It does not say how. Using Git plumbing in a scratch repository is **our** implementation choice, not a description of OpenAI's system. A native Rust delta compressor would remove the `git` dependency from compaction; it is not required for the architecture.

**Configuration:** `PANDO_RECOMPRESS=0` disables recompression (covers stay spliced). `PANDO_COMPACT_GIT` names the `git` binary. If `git` is unavailable, recompression is skipped and the spliced cover is kept; a `cover_recompress` event records the reason.
