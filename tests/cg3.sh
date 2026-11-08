#!/bin/sh
# CG3: immutable packs (never unpacked), dependency closure in tricky histories.
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo r3
U="$URL_BASE/r3.git"
git clone -q "$U" "$W/a"
for i in 1 2 3; do commit_file "$W/a" "f$i" "v$i" "c$i"; done
seq 1 5000 > "$W/a/big"; git -C "$W/a" add big; git -C "$W/a" commit -qm big
git -C "$W/a" push -q origin main
allrefs_ok() { # every advertised ref fully fetchable + fsck in a fresh clone (mirror)
  rm -rf "$W/m.git" && git clone -q --mirror "$U" "$W/m.git" && git -C "$W/m.git" fsck --full --strict >/dev/null &&
  for r in $(git ls-remote "$U" | cut -f2 | grep -v '^HEAD$'); do
    git -C "$W/m.git" rev-parse -q --verify "$r" >/dev/null || { echo "missing $r"; return 1; }
  done
}
# branch from a non-tip commit deep in history (no new objects; empty pack)
git -C "$W/a" branch old HEAD~2
check "push branch at existing non-tip commit (empty pack)" git -C "$W/a" push -q origin old
check "all refs servable" allrefs_ok
# duplicate: same commit on several branches; same pack bytes twice
check "push same commit to 3 branches" git -C "$W/a" push -q origin main:refs/heads/d1 main:refs/heads/d2 main:refs/heads/d3
git -C "$W/a" checkout -qb dup; commit_file "$W/a" dupf x dup
check "push dup branch" git -C "$W/a" push -q origin dup
check "push identical content to another branch" git -C "$W/a" push -q origin dup:refs/heads/dup2
check "noop push" git -C "$W/a" push -q origin dup
# delete and recreate (same and different commit)
check "delete dup" git -C "$W/a" push -q origin :dup
check "recreate dup at same commit" git -C "$W/a" push -q origin dup
check "delete dup again" git -C "$W/a" push -q origin :dup
git -C "$W/a" commit -q --amend -m dup-amended
check "recreate dup at different commit" git -C "$W/a" push -q origin dup
check "all refs servable after delete/recreate" allrefs_ok
# force push rewriting history on main and a topic
git clone -q "$U" "$W/b"
git -C "$W/a" checkout -q main; git -C "$W/a" reset -q --hard HEAD~1; commit_file "$W/a" g rewritten rewritten
check "force push main" git -C "$W/a" push -q -f origin main
check "fetch after force push" git -C "$W/b" fetch -q
check "fetched forced tip" test "$(git -C "$W/b" rev-parse origin/main)" = "$(git -C "$W/a" rev-parse main)"
check "fsck after forced fetch" git -C "$W/b" fsck --full --strict
check "all refs servable after force push" allrefs_ok
# cherry-pick of a blob only present on another branch
git -C "$W/a" checkout -qb onlyhere; printf 'unique-content-%s\n' $$ > "$W/a/uniq"; git -C "$W/a" add uniq; git -C "$W/a" commit -qm uniq
check "push onlyhere" git -C "$W/a" push -q origin onlyhere
git -C "$W/a" checkout -q main; git -C "$W/a" cherry-pick onlyhere >/dev/null
check "push cherry-pick (blob from another branch)" git -C "$W/a" push -q origin main
rm -rf "$W/single"; git clone -q --single-branch -b main "$U" "$W/single"
check "single-branch clone of main fsck" git -C "$W/single" fsck --full --strict
# tags: annotated on old commit, tag of a tag, delete tag
git -C "$W/a" tag -a -m old-ann vold HEAD~3
git -C "$W/a" tag -a -m tagtag vtt vold
check "push tags" git -C "$W/a" push -q origin vold vtt
check "all refs servable with tags" allrefs_ok
check "delete tag" git -C "$W/a" push -q origin :refs/tags/vtt
# incremental fetch into an existing clone that only has some refs
rm -rf "$W/inc"; git clone -q --single-branch -b old "$U" "$W/inc"
check "fetch all into partial clone" git -C "$W/inc" fetch -q origin '+refs/heads/*:refs/remotes/origin/*'
check "fsck partial->full" git -C "$W/inc" fsck --full --strict
# structural: never unpacked into a git repository
check "no git object store under data dir" sh -c "! find '$G_DATA_DIR' -type d \( -name objects -o -name refs \) | grep -q . && ! find '$G_DATA_DIR' -name HEAD -o -name 'config' | grep -q ."
check "no loose objects (zlib files named by 38 hex)" sh -c "! find '$G_DATA_DIR' -type f | grep -Eq '/[0-9a-f]{2}/[0-9a-f]{38}$'"
# packs immutable: file name == trailer checksum == sha1 of content; read-only
verify_packs() {
  for p in "$G_DATA_DIR"/blobs/repos/*/packs/*.pack; do
    id=$(basename "$p" .pack)
    sz=$(wc -c < "$p")
    trailer=$(tail -c 20 "$p" | xxd -p | tr -d '\n')
    body=$(head -c $((sz-20)) "$p" | shasum | cut -d' ' -f1)
    [ "$trailer" = "$id" ] && [ "$body" = "$id" ] || { echo "bad $p"; return 1; }
    [ -w "$p" ] && { echo "writable $p"; return 1; }
    git index-pack --verify "$p" 2>/dev/null || true
  done
}
check "stored packs are byte-identical, checksummed, read-only" verify_packs
find "$G_DATA_DIR/blobs" -name '*.pack' -exec shasum {} \; | sort > "$W/before.sum"
"$RUN" maintain >/dev/null
find "$G_DATA_DIR/blobs" -name '*.pack' -exec shasum {} \; | sort > "$W/after.sum"
check "maintain never modifies or deletes packs" sh -c "comm -23 '$W/before.sum' '$W/after.sum' | wc -l | grep -q '^ *0$'"
check "all refs servable after maintain" allrefs_ok
kill_server; start_server
check "all refs servable after restart" allrefs_ok
# multi-ref push (one pack, two refs) + client prunes the unreachable half + later fetch
rm -rf "$W/mr" "$W/mx"; git clone -q "$U" "$W/mr"
git -C "$W/mr" checkout -qb mra; commit_file "$W/mr" ma "$$ a" ma
git -C "$W/mr" checkout -qb mrb main; commit_file "$W/mr" mb "$$ b" mb
check "push two new branches in one pack" git -C "$W/mr" push -q origin mra mrb
git clone -q --single-branch -b mra "$U" "$W/mx"
git -C "$W/mx" -c gc.reflogExpire=now -c gc.reflogExpireUnreachable=now gc -q --prune=now 2>/dev/null
git -C "$W/mr" checkout -qb mrc mrb; commit_file "$W/mr" mc c mc
check "push branch built on the other half" git -C "$W/mr" push -q origin mrc
check "pruned single-branch client can fetch it" sh -c "git -C '$W/mx' fetch -q origin mrc:refs/remotes/origin/mrc && git -C '$W/mx' fsck --full --strict"
# found by tests/model_check.py: one push force-moves main to unrelated history AND creates a
# topic on top of the old main; a single-branch client of main that gc'ed must still fetch topic
rm -rf "$W/lk" "$W/lx"; git clone -q "$U" "$W/lk"
git -C "$W/lk" checkout -qb lktopic; commit_file "$W/lk" lt "$$ topic" lt
git -C "$W/lk" checkout -q --orphan lkroot; git -C "$W/lk" rm -rqf . ; commit_file "$W/lk" root "$$ root" root
git -C "$W/lk" branch -f main lkroot; git -C "$W/lk" checkout -q main
check "one push: force main to unrelated root + new topic on old main" git -C "$W/lk" push -q -f origin main lktopic
git clone -q --single-branch -b main "$U" "$W/lx"
git -C "$W/lx" -c gc.reflogExpire=now -c gc.reflogExpireUnreachable=now gc -q --prune=now 2>/dev/null
check "gc'ed single-branch client fetches the topic" sh -c "git -C '$W/lx' fetch -q origin lktopic:refs/remotes/origin/lktopic && git -C '$W/lx' fsck --full --strict"
