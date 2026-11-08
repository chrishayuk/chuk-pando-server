#!/bin/sh
# Metadata operations per push vs push size; per-fetch planning cost. Prints a table.
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo m
U="$URL_BASE/m.git"
git clone -q "$U" "$W/a"
commit_file "$W/a" base base base; git -C "$W/a" push -q origin main
stat() { curl -s "$URL_BASE/_stats" | tr ',{}' '\n\n\n' | grep "\"$1\"" | cut -d: -f2; }
measure() { # label cmd...
  label="$1"; shift
  r0=$(stat metadata_reads); w0=$(stat metadata_writes); b0=$(stat bytes_in)
  "$@" >/dev/null 2>&1 || echo "  (command failed: $label)"
  r1=$(stat metadata_reads); w1=$(stat metadata_writes); b1=$(stat bytes_in)
  printf '%-44s reads=%-4s writes=%-4s bytes_in=%s\n' "$label" $((r1-r0)) $((w1-w0)) $((b1-b0))
  echo "$label $((r1-r0)) $((w1-w0))" >> "$W/m.txt"
}
git -C "$W/a" checkout -qb small; commit_file "$W/a" s 1 s
measure "push: 1 commit, new branch" git -C "$W/a" push -q origin small
git -C "$W/a" checkout -qb many
for i in $(seq 1 1000); do echo "$i" > "$W/a/m$((i % 50))"; git -C "$W/a" add -A; git -C "$W/a" commit -qm "m$i"; done
measure "push: 1000 commits, new branch" git -C "$W/a" push -q origin many
git -C "$W/a" checkout -qb big; head -c 20000000 /dev/urandom > "$W/a/big"; git -C "$W/a" add big; git -C "$W/a" commit -qm big
measure "push: 20 MB blob, new branch" git -C "$W/a" push -q origin big
commit_file "$W/a" u 1 u
measure "push: 1 commit, update existing branch" git -C "$W/a" push -q origin big
measure "push: delete branch" git -C "$W/a" push -q origin :small
measure "clone (all branches)" git clone -q "$U" "$W/c"
commit_file "$W/a" v 2 v; git -C "$W/a" push -q origin big
measure "incremental fetch (1 new version)" git -C "$W/c" fetch -q
# every push (1 commit .. 1000 commits .. 20 MB): <= 6 reads (first push also loads the object index) and exactly 4 writes
check "push metadata ops independent of push size" sh -c "grep '^push: .*new branch\|^push: 1 commit, update' '$W/m.txt' | awk '{r=\$(NF-1); w=\$NF; if (r>6 || w!=4) bad=1} END{exit bad}'"
cat "$W/m.txt"
