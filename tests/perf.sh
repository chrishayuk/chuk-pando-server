#!/bin/sh
# Realistic size: import reference/git (the Git source tree) as a repository with a few
# hundred commits of real edits, push it, clone it, fetch increments. Prints timings + stats.
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo perf
U="$URL_BASE/perf.git"
git init -q "$W/src"
rsync -a --exclude .git "$ROOT/reference/git/" "$W/src/"
git -C "$W/src" add -A >/dev/null 2>&1; git -C "$W/src" commit -qm import
# 200 commits editing real files (realistic deltas)
files=$(cd "$W/src" && ls *.c | head -200)
n=0
for f in $files; do n=$((n+1)); echo "/* edit $n */" >> "$W/src/$f"; git -C "$W/src" commit -qam "edit $n $f"; done
git -C "$W/src" remote add origin "$U"
t0=$(date +%s)
check "push full history ($(git -C "$W/src" count-objects -vH | grep size-pack))" git -C "$W/src" push -q origin main
t1=$(date +%s)
check "clone + fsck" fsck_clone "$U" "$W/c1"
t2=$(date +%s)
for i in 1 2 3 4 5; do echo "/* more $i */" >> "$W/src/git.c"; git -C "$W/src" commit -qam "more $i"; git -C "$W/src" push -q origin main; done
t3=$(date +%s)
check "incremental fetch" git -C "$W/c1" pull -q --ff-only
t4=$(date +%s)
"$RUN" maintain >/dev/null
t5=$(date +%s)
check "uri clone after maintain + fsck" sh -c "git -c fetch.uriprotocols=http clone -q '$U' '$W/c2' && git -C '$W/c2' fsck --full --strict"
t6=$(date +%s)
echo "# push $((t1-t0))s, clone+fsck $((t2-t1))s, 5 pushes $((t3-t2))s, fetch $((t4-t3))s, maintain $((t5-t4))s, uri clone+fsck $((t6-t5))s"
echo "# $(curl -s "$URL_BASE/_stats")"
du -sh "$G_DATA_DIR/blobs" | sed 's/^/# blob store: /'
