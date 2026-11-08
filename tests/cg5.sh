#!/bin/sh
# CG5: explicit haves with the modified client (reference/openai-git built into $OGIT_DIR),
# plus parallel packfile-uri download/indexing (CG6 client side).
. "$(dirname "$0")/lib.sh"
OG="${OGIT:-$TMPDIR/ogit/src/bin-wrappers/git}"
[ -x "$OG" ] || { echo "modified git not built at $OG"; exit 1; }
trap 'finish' EXIT
start_server
"$RUN" create-repo r5
U="$URL_BASE/r5.git"
EV="$G_DATA_DIR/events/events.jsonl"
"$OG" clone -q "$U" "$W/a"
for i in 1 2 3; do commit_file "$W/a" "m$i" "$i" "m$i"; done
"$OG" -C "$W/a" push -q origin main
# another (stock) branch on the server that the explicit-haves client must not depend on
git clone -q "$U" "$W/s"; git -C "$W/s" checkout -qb other; commit_file "$W/s" o o other; git -C "$W/s" push -q origin other
"$OG" -C "$W/a" fetch -q
"$OG" -C "$W/a" checkout -qb topic
commit_file "$W/a" t1 t1 t1
check "explicit-haves push new topic" "$OG" -C "$W/a" push -q origin topic
check "server saw explicit-haves" sh -c "grep '\"ev\":\"push_received\"' '$EV' | tail -1 | grep -q '\"explicit_haves\":true'"
dep1=$(grep '"ev":"ref_version_committed"' "$EV" | grep 'refs/heads/topic' | tail -1)
check "topic@1 depends on trunk only" sh -c "echo '$dep1' | grep -o '\"deps\":\[[^]]*\]' | grep -q 'refs/heads/main' && ! echo '$dep1' | grep -o '\"deps\":\[[^]]*\]' | grep -q 'other'"
commit_file "$W/a" t2 t2 t2
check "second topic push" "$OG" -C "$W/a" push -q origin topic
ing=$(grep '"ev":"pack_ingested"' "$EV" | tail -1 | grep -o '"objects":[0-9]*' | cut -d: -f2)
check "second push re-sends earlier topic commits not in trunk (objects=$ing >= 6)" test "$ing" -ge 6
dep2=$(grep '"ev":"ref_version_committed"' "$EV" | grep 'refs/heads/topic' | tail -1)
check "topic@2 pack depends on trunk only (reusable by any reader with trunk)" sh -c "echo '$dep2' | grep -o '\"deps\":\[[^]]*\]' | grep -vq 'refs/heads/topic'"
check "pushed L0 pack has no REF_DELTA (no-ref-delta honoured)" sh -c "grep '\"ev\":\"pack_ingested\"' '$EV' | tail -1 | grep -q '\"thin_bases\":0'"
# update checks still work: non-fast-forward rejected by the client, force-with-lease honoured
git clone -q -b topic "$U" "$W/b"; commit_file "$W/b" t3 other-writer t3; git -C "$W/b" push -q origin topic
commit_file "$W/a" t4 t4 t4
check_fail "stale non-ff push rejected" "$OG" -C "$W/a" push origin topic
lease=$("$OG" -C "$W/a" rev-parse origin/topic)
check_fail "force-with-lease with stale lease rejected" "$OG" -C "$W/a" push --force-with-lease=topic:$lease origin topic
"$OG" -C "$W/a" fetch -q
check "force-with-lease with fresh lease accepted" "$OG" -C "$W/a" push -q --force-with-lease origin topic
check "delete topic with explicit-haves client" "$OG" -C "$W/a" push -q origin :topic
check "recreate topic" "$OG" -C "$W/a" push -q origin topic
check "stock clone + fsck sees everything" fsck_clone "$U" "$W/c"
# exactly-one-winner with explicit-haves clients on a non-trunk branch
"$OG" clone -q -b topic "$U" "$W/x1"; "$OG" clone -q -b topic "$U" "$W/x2"
commit_file "$W/x1" r 1 r1; commit_file "$W/x2" r 2 r2
("$OG" -C "$W/x1" push -q origin topic >/dev/null 2>&1; echo $? > "$W/x1.rc") &
("$OG" -C "$W/x2" push -q origin topic >/dev/null 2>&1; echo $? > "$W/x2.rc") &
wait
check "explicit-haves race: exactly one winner" sh -c "[ \$(cat '$W/x1.rc')\$(cat '$W/x2.rc') = 01 ] || [ \$(cat '$W/x1.rc')\$(cat '$W/x2.rc') = 10 ]"
# parallel packfile-uri download and indexing (modified client, no-ref-delta)
for i in $(seq 1 40); do commit_file "$W/a" "p$i" "$i" "p$i"; "$OG" -C "$W/a" push -q origin HEAD:main 2>/dev/null || { "$OG" -C "$W/a" pull -q --rebase origin main; "$OG" -C "$W/a" push -q origin HEAD:main; }; done
"$RUN" maintain >/dev/null
check "parallel packfile-uri clone (fetch.packfileUriJobs=4)" sh -c "'$OG' -c fetch.uriprotocols=http -c fetch.packfileUriJobs=4 clone -q '$U' '$W/par' && '$OG' -C '$W/par' fsck --full --strict"
line=$(grep '"ev":"fetch_served"' "$EV" | tail -1)
nuri=$(echo "$line" | grep -o '"uri_packs":\[[^]]*\]' | tr ',' '\n' | grep -c '[0-9a-f]\{40\}')
echo "# uri packs in parallel clone: $nuri"
check "several packs served by URI ($nuri)" test "$nuri" -ge 2
check "parallel client requested no-ref-delta + packfile-uris" sh -c "echo '$line' | grep -q '\"no_ref_delta\":true' && echo '$line' | grep -q '\"packfile_uris\":true'"
