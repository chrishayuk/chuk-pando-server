#!/bin/sh
# Git feature matrix against a running server (feeds FEATURES.md).
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo feat
U="$URL_BASE/feat.git"
git clone -q "$U" "$W/a"
for i in 1 2 3; do commit_file "$W/a" "f$i" "$i" "c$i"; done
git -C "$W/a" push -q origin main
check "protocol v0 clone" sh -c "git -c protocol.version=0 clone -q '$U' '$W/v0' && git -C '$W/v0' fsck --full --strict"
check "protocol v1 clone" sh -c "git -c protocol.version=1 clone -q '$U' '$W/v1' && git -C '$W/v1' fsck --full --strict"
commit_file "$W/a" f4 4 c4; git -C "$W/a" push -q origin main
check "protocol v0 incremental fetch" sh -c "git -C '$W/v0' -c protocol.version=0 pull -q --ff-only && git -C '$W/v0' fsck --full --strict && [ \$(git -C '$W/v0' rev-parse HEAD) = \$(git -C '$W/a' rev-parse HEAD) ]"
check "protocol v2 incremental fetch" sh -c "git -C '$W/v1' -c protocol.version=2 pull -q --ff-only && [ \$(git -C '$W/v1' rev-parse HEAD) = \$(git -C '$W/a' rev-parse HEAD) ]"
git -C "$W/a" tag -a -m t1 t1
commit_file "$W/a" f5 5 c5
check "push --follow-tags" git -C "$W/a" push -q --follow-tags origin main
check "tag auto-follow on plain fetch" sh -c "git -C '$W/v1' fetch -q && git -C '$W/v1' rev-parse -q --verify refs/tags/t1"
check "clone --bare" sh -c "git clone -q --bare '$U' '$W/bare.git' && git -C '$W/bare.git' fsck --full --strict"
check "clone --mirror" sh -c "git clone -q --mirror '$U' '$W/mir.git' && git -C '$W/mir.git' fsck --full --strict"
git -C "$W/a" checkout -qb x1; commit_file "$W/a" x 1 x1; git -C "$W/a" push -q origin x1
git -C "$W/a" checkout -q main
check "push --mirror (creates/deletes to match)" sh -c "git -C '$W/mir.git' push -q --mirror '$U'"
check "fetch --prune" sh -c "git -C '$W/v1' fetch -q --prune"
check "push -o (push options accepted, ignored)" git -C "$W/a" push -q -o ci.skip origin main
check "push --dry-run" git -C "$W/a" push -q --dry-run origin main:refs/heads/dry
check "push --atomic two refs" git -C "$W/a" push -q --atomic origin main:refs/heads/at1 main:refs/heads/at2
check "ls-remote --symref HEAD" sh -c "git ls-remote --symref '$U' HEAD | grep -q 'ref: refs/heads/main'"
check "git remote show" git -C "$W/a" remote show origin
check "pull --rebase" sh -c "git -C '$W/v0' pull -q --rebase"
# gitlink (submodule pointer) in a tree
sub=$(git -C "$W/a" rev-parse HEAD)
git -C "$W/a" update-index --add --cacheinfo 160000,"$(printf '%040d' 1 | tr 0 a)",sub
git -C "$W/a" commit -qm gitlink
check "push tree containing a gitlink" git -C "$W/a" push -q origin main
check "clone tree with gitlink + fsck" sh -c "git clone -q --no-recurse-submodules '$U' '$W/gl' && git -C '$W/gl' fsck --full"
# large file
head -c 20000000 /dev/urandom > "$W/a/large"; git -C "$W/a" add large; git -C "$W/a" commit -qm large
check "push 20MB object (chunked request)" git -C "$W/a" push -q origin main
check "clone with large object + fsck" fsck_clone "$U" "$W/lg"
# many haves -> gzip-compressed upload-pack requests
git -C "$W/v0" checkout -qb many; for i in $(seq 1 300); do git -C "$W/v0" commit -q --allow-empty -m "e$i"; done
check "fetch with many local-only commits (gzip negotiation)" git -C "$W/v0" fetch -q origin
check "fetch by exact tip sha" sh -c "git -C '$W/v0' fetch -q origin \$(git -C '$W/a' rev-parse HEAD)"
check "fetch by non-tip historical sha (v2 any want)" sh -c "git init -q '$W/anysha' && git -C '$W/anysha' fetch -q '$U' \$(git -C '$W/a' rev-parse HEAD~3) && git -C '$W/anysha' cat-file -e \$(git -C '$W/a' rev-parse HEAD~3)"
check "partial clone request falls back to full clone" sh -c "git clone -q --filter=blob:none '$U' '$W/pc' 2>/dev/null && git -C '$W/pc' fsck --full"
check "shallow clone --depth 1" sh -c "git clone -q --depth 1 '$U' '$W/sh' && [ \$(git -C '$W/sh' rev-list --count HEAD) = 1 ] && git -C '$W/sh' fsck --full >/dev/null"
commit_file "$W/a" s1 1 s1; commit_file "$W/a" s2 2 s2; git -C "$W/a" push -q origin main
check "fetch into shallow clone" sh -c "git -C '$W/sh' pull -q --ff-only && [ \$(git -C '$W/sh' rev-parse HEAD) = \$(git -C '$W/a' rev-parse HEAD) ] && git -C '$W/sh' fsck --full >/dev/null"
check "deepen shallow clone (fetch --depth 4)" sh -c "git -C '$W/sh' fetch -q --depth 4 && [ \$(git -C '$W/sh' rev-list --count HEAD) = 4 ]"
check "unshallow (fetch --unshallow)" sh -c "git -C '$W/sh' fetch -q --unshallow && [ \$(git -C '$W/sh' rev-list --count HEAD) = \$(git -C '$W/a' rev-list --count HEAD) ] && git -C '$W/sh' fsck --full >/dev/null"
check "shallow clone of a branch with depth 2" sh -c "git clone -q --depth 2 -b at1 '$U' '$W/sh2' && [ \$(git -C '$W/sh2' rev-list --count HEAD) = 2 ]"
check_fail "shallow-since (unsupported, clear error)" git clone -q --shallow-since=2000-01-01 "$U" "$W/sh3"
check "dumb http refused (403)" sh -c "curl -s -o /dev/null -w '%{http_code}' '$URL_BASE/feat.git/info/refs' | grep -q 403"
check "unknown repo 404" sh -c "! git ls-remote '$URL_BASE/nope.git' 2>/dev/null"
check "_stats JSON" sh -c "curl -s '$URL_BASE/_stats' | grep -q metadata_reads"
# directory/file ref conflicts are rejected like git (refs/heads/df vs refs/heads/df/x)
git -C "$W/a" push -q origin HEAD:refs/heads/df
check_fail "D/F conflict: cannot create refs/heads/df/x while refs/heads/df exists" git -C "$W/a" push origin HEAD:refs/heads/df/x
git -C "$W/a" push -q origin HEAD:refs/heads/fd/x
check_fail "D/F conflict: cannot create refs/heads/fd while refs/heads/fd/x exists" git -C "$W/a" push origin HEAD:refs/heads/fd
git -C "$W/a" push -q origin :refs/heads/df
check "after deleting refs/heads/df, refs/heads/df/x can be created" git -C "$W/a" push -q origin HEAD:refs/heads/df/x
