#!/bin/sh
# CG1: stock git clone/push/fetch over HTTP protocol v2.
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo r1
U="$URL_BASE/r1.git"

check "clone empty repo" git clone -q "$U" "$W/a"
commit_file "$W/a" f1 hello "c1"
check "push first commit to main" git -C "$W/a" push -q origin main
check "clone populated" fsck_clone "$U" "$W/b"
check "content matches" test "$(cat "$W/b/f1")" = hello
for i in 2 3 4; do commit_file "$W/a" "f$i" "v$i" "c$i"; done
check "push updates" git -C "$W/a" push -q origin main
check "fetch incrementally" git -C "$W/b" pull -q --ff-only
check "fsck after fetch" git -C "$W/b" fsck --full --strict
check "head matches" test "$(git -C "$W/a" rev-parse HEAD)" = "$(git -C "$W/b" rev-parse HEAD)"
git -C "$W/a" checkout -qb topic
commit_file "$W/a" t1 topic "t1"
check "push new branch" git -C "$W/a" push -q origin topic
commit_file "$W/a" t2 topic2 "t2"
check "push branch update" git -C "$W/a" push -q origin topic
check "fetch new branch" git -C "$W/b" fetch -q origin
check "fetched topic tip" test "$(git -C "$W/b" rev-parse origin/topic)" = "$(git -C "$W/a" rev-parse topic)"
check "fsck b" git -C "$W/b" fsck --full --strict
# bigger content with deltas
seq 1 20000 > "$W/a/big"; git -C "$W/a" add big; git -C "$W/a" commit -qm big
sed -i.bak 's/^1000$/x/' "$W/a/big"; rm -f "$W/a/big.bak"; git -C "$W/a" commit -qam big2
check "push deltas" git -C "$W/a" push -q origin topic
check "clone all + fsck" fsck_clone "$U" "$W/c"
check "c has topic" test "$(git -C "$W/c" rev-parse origin/topic)" = "$(git -C "$W/a" rev-parse topic)"
# tags
git -C "$W/a" tag light; git -C "$W/a" tag -a -m ann v1.0
check "push tags" git -C "$W/a" push -q origin --tags
check "fetch tags" git -C "$W/b" fetch -q --tags origin
check "annotated tag peeled" test "$(git -C "$W/b" rev-parse 'v1.0^{commit}')" = "$(git -C "$W/a" rev-parse topic)"
check "ls-remote" git ls-remote "$U"
# delete branch
check "delete branch" git -C "$W/a" push -q origin :topic
check "branch gone" sh -c "! git ls-remote '$U' | grep -q refs/heads/topic"
check "fresh clone fsck" fsck_clone "$U" "$W/d"
