#!/bin/sh
# Provenance scan of this repository against Git's GPLv2 sources (docs/PROVENANCE-REVIEW.md section 6).
# Read-only on both trees. Reproduce with:
#   verify/release/provenance/run_scan.sh GIT_SRC [OPENAI_GIT_SRC] OUT_DIR
# GIT_SRC: an upstream Git source tree (the review used the builder's reference/git and reference/openai-git,
# builder workspace, <builder workspace>/reference/). OUT_DIR receives every intermediate file and summary.txt.
set -eu
K="$(cd "$(dirname "$0")" && pwd)"; R="${PANDO_REPO:-$(cd "$K/../../.." && pwd)}"
G="$1"; if [ $# -ge 3 ]; then F="$2"; O="$3"; else F=""; O="$2"; fi
mkdir -p "$O"; S="$O/summary.txt"; : > "$S"
py() { python3 -I "$@"; }
say() { echo "$*" | tee -a "$S"; }
say "repository: $R at $(git -C "$R" rev-parse HEAD)"
say "git sources: $G ${F:+and $F}"
# corpora
( cd "$R" && git ls-files 'server/src/*.rs' ) | sed "s|^|$R/|" > "$O/ours_rs.list"
( cd "$R" && git ls-files '*.py' '*.sh' '*.toml' '*.yml' '*Dockerfile*' | grep -vE '^verify/(heldout|audit/archive|results)/' ) | sed "s|^|$R/|" > "$O/ours_hash.list"
( cd "$R" && git ls-files 'verify/heldout/*.py' 'verify/heldout/*.sh' 'verify/audit/archive/*.py' 'verify/audit/archive/*.sh' ) | sed "s|^|$R/|" > "$O/frozen.list"
find "$G" ${F:+"$F"} \( -name '*.c' -o -name '*.h' -o -name '*.rs' \) -not -path '*/t/*' > "$O/theirs_c.list"
find "$G" ${F:+"$F"} -path '*/t/*' \( -name '*.sh' -o -name '*.pl' \) > "$O/theirs_t.list"
find "$G" ${F:+"$F"} -path '*/Documentation/*' \( -name '*.adoc' -o -name '*.txt' \) -exec cat {} + > "$O/docs.txt" 2>/dev/null || true
xargs python3 -I "$K/extract.py" c    < "$O/ours_rs.list"    > "$O/ours_rs.jsonl"
xargs python3 -I "$K/extract.py" hash < "$O/ours_hash.list"  > "$O/ours_hash.jsonl"
xargs python3 -I "$K/extract.py" hash < "$O/frozen.list"     > "$O/frozen_hash.jsonl"
xargs python3 -I "$K/extract.py" c    < "$O/theirs_c.list"   > "$O/theirs_c.jsonl"
xargs python3 -I "$K/extract.py" hash < "$O/theirs_t.list"   > "$O/theirs_t.jsonl"
for f in ours_rs:ours_rs ours_hash:ours_hash frozen_hash:frozen theirs_c:theirs_c theirs_t:theirs_t; do j=${f%%:*}; l=${f#*:}; say "items $j: $(wc -l < "$O/$j.jsonl" | tr -d ' ') from $(wc -l < "$O/$l.list" | tr -d ' ') files"; done
# (a) comments: shared word n-grams
for n in 8 6 5; do
  py "$K/ngram.py" "$O/ours_rs.jsonl" "$O/theirs_c.jsonl" $n comment "$O/docs.txt" > "$O/rs_vs_c_comment$n.txt" 2>/dev/null
  say "Rust comments vs Git C/Rust comments, shared $n-word runs: $(grep -c '^\S.*words\]' "$O/rs_vs_c_comment$n.txt" || true)"
done
py "$K/ngram.py" "$O/ours_rs.jsonl" "$O/theirs_t.jsonl" 8 comment > "$O/rs_vs_t_comment8.txt" 2>/dev/null
say "Rust comments vs Git t/ comments, shared 8-word runs: $(grep -c 'words\]' "$O/rs_vs_t_comment8.txt" || true)"
for n in 8 6; do
  py "$K/ngram.py" "$O/ours_hash.jsonl" "$O/theirs_c.jsonl" $n comment > "$O/hash_vs_c_comment$n.txt" 2>/dev/null
  py "$K/ngram.py" "$O/ours_hash.jsonl" "$O/theirs_t.jsonl" $n comment > "$O/hash_vs_t_comment$n.txt" 2>/dev/null
  say "Python/shell comments vs Git C, shared $n-word runs: $(grep -c 'words\]' "$O/hash_vs_c_comment$n.txt" || true); vs Git t/: $(grep -c 'words\]' "$O/hash_vs_t_comment$n.txt" || true)"
done
for k in comment string; do
  py "$K/ngram.py" "$O/frozen_hash.jsonl" "$O/theirs_t.jsonl" 7 $k > "$O/frozen_vs_t_${k}7.txt" 2>/dev/null
  py "$K/ngram.py" "$O/frozen_hash.jsonl" "$O/theirs_c.jsonl" 7 $k > "$O/frozen_vs_c_${k}7.txt" 2>/dev/null
  say "frozen suites vs Git t/ and C, shared 7-word $k runs: $(grep -c 'words\]' "$O/frozen_vs_t_${k}7.txt" || true) and $(grep -c 'words\]' "$O/frozen_vs_c_${k}7.txt" || true)"
done
# (b) identical string literals of 6+ characters
py "$K/strmatch.py" "$O/ours_rs.jsonl" "$O/theirs_c.jsonl" 6 > "$O/rs_vs_c_strings.txt"
say "Rust string literals (6+ chars) identical to a Git C/Rust literal: $(wc -l < "$O/rs_vs_c_strings.txt" | tr -d ' ') (listed in rs_vs_c_strings.txt)"
# (c) distinctive Git identifiers
for id in PARSE_CP_PARAM cp_off cp_size get_delta_hdr_size DELTA_SIZE_MIN use_pack packet_read rp_error hdr_nocache unpack_object_header_buffer patch_delta; do
  say "identifier $id in server/src: $(grep -rlw "$id" "$R/server/src" | wc -l | tr -d ' ') file(s)"
done
# (d) headers, licences and Git authors' names in tracked files
( cd "$R" && git ls-files -z -- . ':!LICENSE' ':!THIRD-PARTY-LICENSES.txt' | xargs -0 grep -nIiE 'copyright|\(c\) [0-9]{4}|GPL|General Public|SPDX|Junio|Hamano|Torvalds|Nicolas Pitre|Schindelin|signed-off-by|@kernel\.org' -- 2>/dev/null ) > "$O/headers.txt" || true
say "header/name hits in tracked files other than LICENSE and THIRD-PARTY-LICENSES.txt: $(wc -l < "$O/headers.txt" | tr -d ' ') lines, by file:"
cut -d: -f1 "$O/headers.txt" | sort | uniq -c | sort -rn | tee -a "$S"
say "history: commits adding reference/, figures/, BRIEF.md or .c/.h/.adoc files: $(git -C "$R" log --all --diff-filter=A --name-only --format= | grep -cE '^(reference/|figures/|BRIEF\.md)|\.(c|h|adoc)$' || true)"
say "history: commits whose diff adds or removes 'GNU General Public', with the files:"
for c in $(git -C "$R" log --all -S'GNU General Public' --format=%h); do say "  $c $(git -C "$R" show --format=%s --name-only $c | head -1): $(git -C "$R" grep -l 'GNU General Public' $c -- 2>/dev/null | sed "s/^$c://" | tr '
' ' ')"; done
# test vectors: 40/64-hex strings shared with Git's t/
( cd "$R" && git ls-files -z -- server tests verify ':!verify/results' | xargs -0 grep -ohIE '\b[0-9a-f]{64}\b|\b[0-9a-f]{40}\b' 2>/dev/null ) | sort -u > "$O/our_hex.txt"
find "$G" ${F:+"$F"} -path '*/t/*' -type f -exec grep -ohIE '\b[0-9a-f]{64}\b|\b[0-9a-f]{40}\b' {} + 2>/dev/null | sort -u > "$O/t_hex.txt"
comm -12 "$O/our_hex.txt" "$O/t_hex.txt" > "$O/shared_hex.txt"
say "hex vectors: ours $(wc -l < "$O/our_hex.txt" | tr -d ' '), Git t/ $(wc -l < "$O/t_hex.txt" | tr -d ' '), shared $(wc -l < "$O/shared_hex.txt" | tr -d ' ') (shared_hex.txt)"
# binaries among tracked files
( cd "$R" && git ls-files -z | xargs -0 file --mime-encoding ) | grep -v -E ':[[:space:]]+(us-ascii|utf-8)$' > "$O/nontext.txt" || true
say "tracked files that are not plain text: $(wc -l < "$O/nontext.txt" | tr -d ' ') (nontext.txt)"
say "done: $O"
