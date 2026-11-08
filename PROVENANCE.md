# Provenance

**Seed:** gate snapshot of autonomous builder **Condition B**, Pando campaign spec-v1.2.
- Snapshot repo: the builder snapshot repository `b.git` (private experiment record), tag `gate-self-declared-s01`, commit `cab020f68db2d9ce81159e5e00f260d66f926296`.
- Gate tar sha256: `0b640ac73dac9b37b744f0f1ef8b6a6a9f6c8f6499296f14b1331a348120ccd3`.
- The experiment record (`chuk-pando`) was retired to a read-only archive on 2026-11-07. What this repository needs
  from it was copied verbatim: `verify/heldout/` (independent harness and frozen suites), `verify/audit/archive/`
  (re-audit and review, with reproducers), and `docs/record/` (RUNLOG, adjudications, scoring, CG7 summary).

**Seed commit contents:** the snapshot's files, byte-for-byte, **except**:
- `reference/`: upstream Git and OpenAI fork sources (GPLv2). Third-party code, not part of this product.
- `figures/`: images from OpenAI's article. Copyrighted; not ours to redistribute.
- `BRIEF.md`, `acceptance/`: campaign instructions.
- `.builder-home/`: builder git config.

**Authorship:** the code was written by an autonomous coding agent (Claude Opus 5.5, Claude Code) from the published article and the campaign brief, with the upstream Git and OpenAI-fork sources available in its workspace. Changes after the seed commit are ordinary engineering, recorded in git history.

**Licence:** Apache-2.0 (`LICENSE`, `NOTICE`), proposed on branch `release-1` after the provenance and licence review
(`docs/PROVENANCE-REVIEW.md`). That review checked whether any code was derived from the GPLv2 sources the builder had
available, and found none. **Not for publication** until the owner accepts the review's remaining owner items (Q1, Q3).

**Claim boundary:** an independently built implementation of the Pando architecture as described in OpenAI's article "Scaling Git at OpenAI" (2 Nov 2026). Not OpenAI's code. No equivalence with OpenAI's production system or its performance figures is claimed.
