# Provenance and licence review (2026-11-07; engineering review completed on branch `release-1`)

**Status:**
- **The engineering review is complete:** every engineering item has a verdict (§6). Nothing found blocks Apache-2.0.
- **The owner decisions remain:** Q1 (legal) and Q3 (AI authorship and copyright). The licence is not applied to a
  public release until the owner accepts this review at the release checkpoint.
- On `release-1`, `LICENSE` (the canonical Apache-2.0 text from apache.org, md5 `3b83ef96387f14655fc854ddc3c6bd57`),
  `NOTICE` and `THIRD-PARTY-LICENSES.txt` have been added so that the release artifacts can be checked. The
  repository stays private.

This is an engineering record, not legal advice. Where a question is legal, it is marked as such.

## 1. What is being released
- **The release is a single binary, `pando`**, built from `server/` (Rust).
- **The source repository** contains:
  - `server/`;
  - `verify/` (Python and shell tests);
  - `docs/`;
  - `deploy/fly/`;
  - `tests/*.sh`, inherited from the seed build;
  - top-level design notes: `DECISIONS.md`, `MODEL.md`, `LOG.md`, `FEATURES.md`, `README.md`.
- **Not in the repository:** the experiment record (`chuk-pando`), the frozen test suites, the Git and OpenAI-fork
  sources, the article's figures, MinIO, and any Git binary.

## 2. Origin of the code (from `PROVENANCE.md` and the builder transcripts)
- **Seed.** The seed is the gate snapshot of autonomous builder **Condition B** (Claude Opus 5.5 in Claude Code), commit
  `cab020f`, which wrote the server from OpenAI's article and the campaign brief. Later changes are ordinary engineering,
  recorded in git history.
- **GPL sources in the builder's workspace.** The builder had upstream **Git** and **OpenAI's Git fork** (both GPLv2)
  in `reference/`. The seed commit excluded them, along with the article's figures.
- **What the builder did with them.** Its complete transcript (`runs/b/session-01.jsonl` in the private experiment record) shows
  172 shell commands, 16 file reads and 4 greps, and **no sub-agents**. Every interaction with `reference/` was traced,
  including commands run after a `cd` into it:
  - **File-read tool on `reference/`: none.** **Grep tool on it: none.**
  - **Through the shell (16 commands):**
    - listing the trees and diffing the fork against upstream;
    - reading the fork's **protocol documentation** (`gitprotocol-pack.adoc`, `gitprotocol-capabilities.adoc`,
      release notes 2.56.0) and upstream `Documentation/gitprotocol-v2.adoc` (the fetch section);
    - viewing **client-side C**:
      - `send-pack.c` (the fork's diff against upstream, up to 150 lines);
      - `remote-curl.c` lines 490–530;
      - `fetch-pack.c` (about 120 lines in four ranges);
      - greps of `receive-pack.c`, `fetch.c`, `transport.c` and `clone.c` for "explicit-haves", "thin" and
        "packfile-uri";
    - `t/t5308-pack-detect-duplicates.sh` lines 1–80 (a test script);
    - copying the fork's tree to a scratch directory and **building it**, to use as a *test client*.
  - **Assessment (engineering, not legal):**
    - The material read was protocol documentation and client behaviour.
    - The server implements the receiving side, in a different language, with a different storage design (immutable
      packs, a transactional metadata store, covers) that has no counterpart in Git's code.
    - No server-side C (`upload-pack.c`, `index-pack.c`, `pack-objects.c`, the delta code) was read.
- **Code markers.** No GPL or copyright headers, and no author names from Git. One comment cites a Git file:
  `repo.rs:669`, "as in stock Git (receive-pack/refs.c refuse a non-commit…)". It was added later, to describe Git's
  *behaviour*, for a fix to CG7 audit finding D-12.
- **Formats implemented.** Packfile, pack index semantics, delta encoding, pkt-line, smart HTTP and protocol v2. These
  are implemented from the documented formats; this server writes its own index format (`PIDX`), not Git's `.idx`.
  Whether implementing a protocol and file format creates any licence obligation is a **legal question** (§5, Q1).
- **Recompression** shells out to the user's own `git pack-objects`. Nothing from Git is linked or bundled.

## 3. Dependencies compiled into the binary (`cargo metadata`, all targets, normal and build dependencies; 79 crates)
| Licence | Crates |
|---|---|
| MIT OR Apache-2.0, Apache-2.0 OR MIT (and variants with 0BSD, Zlib, Unlicense, LLVM exception, ISC) | 61, including anyhow, flate2, sha1, serde, rustls, memchr |
| MIT | 11: bytes, hyper, hyper-util, http-body(-util), mio, tokio(-macros), simd-adler32, redox_syscall, zmij |
| ISC | rustls-webpki, untrusted |
| Apache-2.0 AND ISC | ring |
| BSD-3-Clause | subtle |
| Zlib | zlib-rs |
| CDLA-Permissive-2.0 | webpki-roots (the Mozilla root certificate data) |
| (MIT OR Apache-2.0) AND Unicode-3.0 | unicode-ident |

- All of these are permissive and compatible with distributing an Apache-2.0 binary.
- **Obligation:** ship their licence texts and notices with the binary. Plan: `cargo about generate`, producing
  `THIRD-PARTY-LICENSES.html` in every archive, plus the ring/OpenSSL and ISC notices.
- No copyleft dependency is linked.

## 4. Other material
- **Docs quoting OpenAI's article.** The docs paraphrase it and quote it in places. Before publication, check that
  quotations are short, attributed, and limited to what is needed. The article's figures are already excluded.
- **Fly Docker image** (`deploy/fly/Dockerfile`). It installs Debian's `git` (GPLv2) into the runtime image. That is
  fine for a private deployment. **Publishing that image** would make it a distribution of Git, carrying GPL
  source-offer obligations. Plan: don't publish the image, or meet those obligations.
- **Tests.** They run MinIO (AGPLv3) and the OpenAI fork as *external programs* in test environments; neither is
  distributed. The harness library and the frozen suites live outside this repository.
- **Inherited `tests/*.sh`, `LOG.md` and `DECISIONS.md`** were written by the builder agent; they contain no
  third-party code (spot-checked by grep; a full read is part of closing the review).
- **AI authorship.** The code was written mostly by AI agents, under the owner's direction. Which parts of AI-generated
  code are protectable by copyright, and so what an Apache-2.0 licence covers, is a **legal question** (§5, Q3). It
  doesn't block an Apache-2.0 release: the licence grants permissions to the extent of any rights held.

## 5. Open questions (to be closed before publication)
| # | Question | Kind | Proposed way to close it |
|---|---|---|---|
| Q1 | Does implementing Git's documented protocols and formats, after reading protocol documentation and ~200 lines of client C, make the server a derivative of GPLv2 Git? | legal | Owner's judgment, ideally with an adviser, using §2 as the evidence. Engineering view: no code was translated, but this is not ours to conclude. |
| Q2 | A line-level similarity check between `server/src` and Git's C, focusing on the pack and delta code | engineering | Run a cross-language similarity scan (comments and identifiers; structure of delta apply, pack header parsing, pkt-line) and record the result. It is not yet done. |
| Q3 | AI authorship and copyright | legal | Owner decision; state it in the README ("written largely by AI agents under the author's direction"). |
| Q4 | Quotations from OpenAI's article in docs | editorial | Review every quotation before publication. |
| Q5 | Bundled notices | engineering | `cargo about` in the release workflow. Missing notices fail the release gate. |

**Until Q1 and Q3 are accepted by the owner:** nothing is published, and the repository stays private. The status of
each question is in §6.

## 6. Results of the engineering review (2026-11-07, `release-1`)
**How to check every verdict below yourself.** All of it is read-only.
- The scan is `verify/release/provenance/run_scan.sh GIT_SRC [OPENAI_GIT_SRC] OUT_DIR`. Its tools are in the same
  directory: `extract.py` (comment and string lexer), `ngram.py` (shared word runs), `strmatch.py` (identical
  literals) and `quotes.py` (quotation finder).
- **Git sources used:** the builder's own workspace copies of upstream Git and the OpenAI fork
  (`<builder workspace>/reference/git` and `.../reference/openai-git`).
- **Output:** `summary.txt` and one file per check (`rs_vs_c_comment{8,6,5}.txt`, `rs_vs_c_strings.txt`,
  `headers.txt`, `shared_hex.txt`, `nontext.txt`, ...).
- `summary.txt` of the run on commit `13e873f` (2026-11-08), verbatim except that local directory paths are replaced by placeholders (`<repo>`, `<builder workspace>`):

```
repository: <repo> at 13e873f2768b09cc3d6d5687b2e343aa0b54d643
git sources: <builder workspace>/reference/git and <builder workspace>/reference/openai-git
items ours_rs: 2663 from 22 files
items ours_hash: 6827 from 80 files
items frozen_hash: 3700 from 56 files
items theirs_c: 105688 from 1698 files
items theirs_t: 192584 from 2473 files
Rust comments vs Git C/Rust comments, shared 8-word runs: 0
Rust comments vs Git C/Rust comments, shared 6-word runs: 0
Rust comments vs Git C/Rust comments, shared 5-word runs: 5
Rust comments vs Git t/ comments, shared 8-word runs: 0
Python/shell comments vs Git C, shared 8-word runs: 0; vs Git t/: 0
Python/shell comments vs Git C, shared 6-word runs: 0; vs Git t/: 0
frozen suites vs Git t/ and C, shared 7-word comment runs: 0 and 0
frozen suites vs Git t/ and C, shared 7-word string runs: 0 and 0
Rust string literals (6+ chars) identical to a Git C/Rust literal: 124 (listed in rs_vs_c_strings.txt)
identifier PARSE_CP_PARAM in server/src: 0 file(s)
identifier cp_off in server/src: 0 file(s)
identifier cp_size in server/src: 0 file(s)
identifier get_delta_hdr_size in server/src: 0 file(s)
identifier DELTA_SIZE_MIN in server/src: 0 file(s)
identifier use_pack in server/src: 0 file(s)
identifier packet_read in server/src: 0 file(s)
identifier rp_error in server/src: 0 file(s)
identifier hdr_nocache in server/src: 0 file(s)
identifier unpack_object_header_buffer in server/src: 0 file(s)
identifier patch_delta in server/src: 0 file(s)
header/name hits in tracked files other than LICENSE and THIRD-PARTY-LICENSES.txt: 46 lines, by file:
  15 docs/PROVENANCE-REVIEW.md
  14 verify/audit/archive/net/session.jsonl
   6 verify/audit/archive/rev/session.jsonl
   3 PROVENANCE.md
   2 verify/release/third_party_licenses.py
   2 NOTICE
   2 .github/workflows/release.yml
   1 docs/RELEASE-PLAN.md
   1 docs/NETWORK-STORAGE.md
history: commits adding reference/, figures/, BRIEF.md or .c/.h/.adoc files: 0
history: commits whose diff adds or removes 'GNU General Public', with the files:
  71753fa Windows: test the shared-store refusal and its reason; public README, Windows notes, provenance results; gate registers the release smoke suite: docs/PROVENANCE-REVIEW.md 
hex vectors: ours 240, Git t/ 491, shared 4 (shared_hex.txt)
tracked files that are not plain text: 0 (nontext.txt)
done: OUT_DIR
```

Notes on that summary:
- **Header hits.** Every line of `headers.txt` was read. They are:
  - this review and `PROVENANCE.md` (discussion of the GPL);
  - `NOTICE` (its copyright line, and the statement that Git is GPLv2 and not included);
  - `verify/release/third_party_licenses.py` (an SPDX comment, and the licence-file name pattern);
  - the cargo-dist header in `.github/workflows/release.yml` ("Copyright 2022-2024, axodotdev", "MIT or
    Apache-2.0");
  - `docs/RELEASE-PLAN.md:13` (the licence decision row);
  - `docs/NETWORK-STORAGE.md:12` (MinIO's AGPLv3, as a separate test process);
  - 20 lines in `verify/audit/archive/{net,rev}/session.jsonl`. These are case-insensitive matches inside
    base64-encoded data (for example "...gpLES..."). A case-sensitive search of both transcripts for `GPL`,
    `GPLv2`, `Copyright`, `Junio`, `Torvalds`, `SPDX` and `General Public` finds nothing.
- **History.** The one commit whose diff adds "GNU General Public" is `71753fa`. That is this review's own text in
  `docs/PROVENANCE-REVIEW.md` (and `NOTICE`, added in the same series).
- **String-literal matches.** `rs_vs_c_strings.txt` has 124 matches. The independent first scan counted 123: the
  difference is the Windows port's new strings. Each match is listed with its Git location, and the classes are
  described under Q2 below.
- `LICENSE` and `THIRD-PARTY-LICENSES.txt` are excluded from the header grep, because they are licence texts by
  design.

### Q2: cross-language similarity scan against Git and the OpenAI fork. Verdict: **clean**
- **Corpora.**
  - Every comment and string literal in `server/src/*.rs` (675 comment items), and every tracked `.py`, `.sh`, `.toml`,
    `.yml` and Dockerfile outside `verify/heldout`, `verify/audit/archive` and `verify/results`.
  - Compared with every `*.c`, `*.h` and `*.rs` in upstream Git and the fork, outside `t/` (105,688 items), including
    upstream Git's own Rust (`src/varint.rs`, `src/hash.rs`, ...).
  - Git's `t/**/*.sh,*.pl` was scanned separately (189,264 items).
  - A lexer extracts comments and strings. Shared word n-grams are found after lower-casing; runs that are also in
    Git's `Documentation/` are marked as protocol vocabulary. The tool was checked on a known positive.
- **Comments.**
  - Shared 8-grams: 0. Shared 6-grams: 0.
  - Shared 5-grams: 5, all everyday English ("may or may not be", "is stored as a delta", ...).
  - Python and shell comments against Git's C and `t/`: 0 shared 6-grams.
- **Strings.** 123 exact matches of 6 or more characters. Each is one of:
  - protocol-mandated: capability names, `version 2`, `command=`, `acknowledgments`, `packfile`, `shallow-info`, ...;
  - the arguments of the `git pack-objects` command the server runs;
  - generic HTTP or OS strings.
- **Client-visible reason texts kept identical to Git's, for client compatibility (short and functional):**
  - "funny refname" (`proto.rs`);
  - "deleting a non-existent ref";
  - "atomic push failed";
  - "missing necessary objects" (`ingest.rs`);
  - "failed to update ref: trying to write non-commit object ... to branch '...'" (`repo.rs`, audit D-12);
  - "cannot lock ref '...': reference already exists / unable to resolve reference / is at ... but expected ...";
  - "no-cache, max-age=0, must-revalidate".

  **Verdict:** retained. They are 2–9 word messages that Git's clients and users recognise, with no Git code
  structure around them. The owner may reword the ref-lock messages, which are not wire-critical, if preferred.
- **Identifiers.** None of Git's distinctive names appear: `PARSE_CP_PARAM`, `cp_off`, `get_delta_hdr_size`,
  `DELTA_SIZE_MIN`, `use_pack`, `packet_read`, `rp_error`, `hdr_nocache`. The shared names are std, flate2 or
  protocol tokens.
- **Structure.** Delta application (`pack.rs` `apply_delta`, `delta_varint`) was compared with `patch-delta.c` and
  `delta.h`:
  - different control flow: bit loops instead of seven `PARSE_CP_PARAM` macros, and an output-length check instead of
    a remaining-size counter;
  - different guards and error texts;
  - the shared parts (copy bit 0x80, the 0x10000 size rule, opcode 0 reserved) are fixed by
    `Documentation/gitformat-pack.adoc`.

  The object-header and OFS encode/decode loops, pkt-line, ls-refs and fetch were compared in the same way, and
  are independent implementations of the documented format. The server never creates deltas; recompression runs
  `git pack-objects`.
- **Headers and names.**
  - `git ls-files | xargs grep` for copyright, GPL, SPDX, Git authors' names and `Signed-off-by` found only:
    - the generated cargo-dist workflow header (`.github/workflows/release.yml`, MIT or Apache-2.0, axodotdev;
      kept as generated);
    - the article's authors named as attribution in `docs/record/01-claims-ledger.md`.
  - None of the 119 commits in history contains `reference/`, `figures/`, `BRIEF.md`, a `.c`, `.h` or `.adoc` file,
    an image, or the text "GNU General Public".

### Q2b: test fixtures and vectors. Verdict: **clean**
- Every 40- and 64-hex string in tracked sources (569) was compared with Git's `t/` (489). Four are shared:
  - the zero OID;
  - three NIST FIPS 180 SHA-256 vectors (`""`, `"abc"`, a million `a`) in `s3.rs` tests.
- The other vectors are from public standards or vendor documentation:
  - RFC 4231 (HMAC);
  - the AWS SigV4 documentation example, labelled as such;
  - RFC 4648 (base64).
- No pack bytes or binary fixtures. Every pack is produced at run time by `git` or the simulator.
- The frozen suites (`verify/heldout`, `verify/audit/archive`) against Git's `t/` and C, as 7-grams of comments and
  strings: 0 shared.

### Vendored, generated and binary files. Verdict: **clean**
- `file --mime` over all tracked files: no binaries, images, PDFs, packs or archives.
- Generated files:
  - `.github/workflows/release.yml` and `dist-workspace.toml` (cargo-dist);
  - `server/Cargo.lock`;
  - `verify/results/**` (test records);
  - `docs/evidence/**` (measurements).
- No vendored third-party code. `verify/audit/archive/rev/work/instr/server/**` is an instrumented copy of this
  server's own earlier source.

### Inherited `tests/*.sh`, `LOG.md`, `DECISIONS.md`. Verdict: **clean**
- They were read in full. They are original project scripts and narrative: plain `git` CLI calls and project event-log
  assertions. They don't use Git's `test_expect_success` framework or any of its test cases.
- `tests/perf.sh` copies a `reference/git` tree in as *workload data* at run time. That path is not in this
  repository, so the script cannot run as is. Nothing from it is embedded.

### Q4: quotations of OpenAI's article. Verdict: **closed (owner review, 2026-11-08)**
**Every remaining quotation is verbatim against the article extract.** Each is short and cites
<https://openai.com/index/scaling-git-at-openai/> (the ledger's source line, or the README link). They are all in
`docs/record/01-claims-ledger.md` unless a file is named:

| # | Quotation | Words |
|---|---|---|
| 1 | "by nearly 5x" | 3 |
| 2 | "nearly 80%" | 2 |
| 3 | "nearly a petabyte of Git data per day" | 8 |
| 4 | "with just a handful of CPU cores" | 7 |
| 5 | "at times falling below 90%" | 5 |
| 6 | "theory of Pando" | 3 |
| 7 | "prove properties" | 2 |
| 8 | "Synthetic teaching models, not production benchmarks." | 6 |
| 9 | "O(1) metadata reads and writes" (`docs/LARGE-PUSH.md`) | 5 |
| 10 | "horizontally scalable" (`docs/record/CG7-SUMMARY-AND-GAPS.md`) | 2 |

**Not quotations:**
- "Scaling Git at OpenAI" is the article's title.
- "Explicit haves" (ledger A11) is the feature's name, as the article uses it.

**Rewritten as attributed paraphrases without quotation marks** (they were our paraphrases, not OpenAI's words):
- `docs/AUDIT-NET-REMEDIATION.md` (A4, the ref pointer as the only contended key);
- `DECISIONS.md` D14 (scaling by adding servers, metadata units and blob stores);
- `docs/COMPACTION-BOUNDARY.md` (a worker claims a cover and produces one packfile, which improves compression);
- `docs/RECOMPRESSION-BASELINE.md` (a client may download objects it already has);
- `docs/record/CG7-SUMMARY-AND-GAPS.md` and `docs/PLANNING.md` (covers up to L4);
- `docs/URI-TAIL.md` (moving work to the background);
- `docs/record/CG5-CG6-SCORING.md` and `docs/record/CG7-SUMMARY-AND-GAPS.md` (claim A1, never unpacked into a
  repository).

**Corrected:** the P6 quotation had dropped a word. It now reads "at times falling below 90%", as in the article.

**Frozen third-party text, not editable:** `verify/audit/archive/net/AUDIT.md` (the independent auditor's report)
quotes the claims ledger's wording of A4, "only contended key is the ref pointer", and attributes it to claim A4. It
is a paraphrase in the ledger, not OpenAI's wording.

No blockquote quotes the article. No figure or image from it is in the tree or its history.

### Q5: bundled notices. Verdict: **closed**
- `verify/release/third_party_licenses.py` generates `THIRD-PARTY-LICENSES.txt` from `Cargo.lock`:
  - all 79 crates compiled into the binary, on all five targets (normal and build dependencies);
  - each crate's declared licence;
  - the full text of every licence and notice file it ships, including ring's BoringSSL/OpenSSL and ISC texts and
    webpki-roots' CDLA-Permissive-2.0.
- It **fails** if a licence is outside the allow-list (MIT, Apache-2.0, ISC, BSD-3-Clause, Zlib, 0BSD, Unlicense,
  Unicode-3.0, CDLA-Permissive-2.0, Apache-2.0 WITH LLVM-exception), or if a crate ships no licence text.
- CI checks that the committed file is current (`--check`), and every release archive contains it, with `LICENSE`
  and `NOTICE` (dist-workspace.toml `include`). The smoke test fails an archive that lacks any of them.
- No dependency ships a `NOTICE` file, so nothing must be propagated under Apache-2.0 §4(d).
- **Dependency licences:**

  | Licence | Number of crates |
  |---|---|
  | MIT OR Apache-2.0 (and variants) | 61 |
  | MIT | 11 |
  | ISC | 2 |
  | Apache-2.0 AND ISC (ring) | 1 |
  | BSD-3-Clause (subtle) | 1 |
  | Zlib (zlib-rs) | 1 |
  | CDLA-Permissive-2.0 (webpki-roots) | 1 |
  | (MIT OR Apache-2.0) AND Unicode-3.0 (unicode-ident) | 1 |

  No GPL, AGPL, LGPL, MPL or unknown licence.
- The Windows port adds `windows-sys` 0.61.2 (MIT OR Apache-2.0) as a direct dependency. It was already in the lock
  file through tokio, so no new crate was added.

### Items that need the owner
| # | Item | Recommendation |
|---|---|---|
| Q1 | Legal: is a server implementing Git's documented protocol, after its builder read protocol docs and about 200 lines of client C, a derivative of GPLv2 Git? | Engineering evidence: no code, comments or vectors were copied or translated (§6 Q2). The decision is the owner's, ideally with an adviser. |
| Q3 | AI authorship and copyright | `NOTICE` and the README state that the code was written largely by AI agents under the author's direction. Owner decision (2026-11-08): no personal name. `NOTICE` reads "Copyright 2026 the chuk-pando-server contributors". |
| Q4 | Quotations | Closed: owner review 2026-11-08; the remaining quotations are verbatim, and the paraphrases have no quotation marks (§6 Q4). |
| T1 | `verify/audit/archive/{net,rev}/session.jsonl` are full AI-agent transcripts (2.6 MB and 1.4 MB). They contain local paths and the claims ledger, and no GPL text. | Decide whether they belong in the public tree. They are checksummed frozen archives, so this branch does not change them. |
| T2 | The Fly Docker image installs Debian's `git` (§4). | Don't publish the image, as planned. |
