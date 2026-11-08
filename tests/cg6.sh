#!/bin/sh
# CG6: compaction levels L0..L4, packs retained, log planning cost, packfile URIs.
. "$(dirname "$0")/lib.sh"
V="${V:-300}"
trap 'finish' EXIT
start_server
"$RUN" create-repo r6
U="$URL_BASE/r6.git"
git clone -q "$U" "$W/a"
seq 1 3000 > "$W/a/data"; git -C "$W/a" add data; git -C "$W/a" commit -qm v0; git -C "$W/a" push -q origin main
: > "$W/order"
for i in $(seq 2 "$V"); do
  echo "line $i" >> "$W/a/data"; printf 'v%s\n' "$i" > "$W/a/f$((i % 7))"; git -C "$W/a" add -A; git -C "$W/a" commit -qm "v$i"
  git -C "$W/a" push -q origin main
  git -C "$W/a" rev-parse HEAD >> "$W/order"
done
EV="$G_DATA_DIR/events/events.jsonl"
"$RUN" maintain
sleep 1
levels=$(grep '"ev":"cover_committed"' "$EV" | grep -o '"level":[0-9]' | sort -u | tr '\n' ' ')
echo "# cover levels: $levels"
check "covers at levels 1..4 exist" sh -c "echo '$levels' | grep -q 'level\":1' && echo '$levels' | grep -q 'level\":4'"
npacks=$(ls "$G_DATA_DIR"/blobs/repos/r6/packs/*.pack | wc -l | tr -d ' ')
ncov=$(grep -c '"ev":"cover_committed"' "$EV")
echo "# packs on disk: $npacks (L0 pushes: $V, covers: $ncov)"
check "all L0 packs retained alongside covers" test "$npacks" -ge $((V + ncov - 1))
check "clone after compaction + fsck" fsck_clone "$U" "$W/full"
git clone -q --mirror "$U" "$W/mirror.git"
# planning cost vs distance (client has version V-d, wants V)
for d in 1 2 4 8 16 32 64 128 256; do
  [ "$d" -lt "$V" ] || continue
  old=$(sed -n "$((V - 1 - d))p" "$W/order")
  rm -rf "$W/dist"; git init -q "$W/dist"
  git -C "$W/dist" fetch -q "$W/mirror.git" "$old:refs/remotes/origin/main"
  git -C "$W/dist" remote add origin "$U"
  git -C "$W/dist" fetch -q origin
  line=$(grep '"ev":"fetch_served"' "$EV" | tail -1)
  reads=$(echo "$line" | grep -o '"plan_metadata_reads":[0-9]*' | cut -d: -f2)
  steps=$(echo "$line" | grep -o '"plan_steps":[0-9]*' | cut -d: -f2)
  covers=$(echo "$line" | grep -o '"covers_used":[0-9]*' | cut -d: -f2)
  ib=$(echo "$line" | grep -o '"inline_bytes":[0-9]*' | cut -d: -f2)
  sub=$(echo "$line" | grep -o '"l0_substituted":[0-9]*' | cut -d: -f2)
  echo "# distance $d: plan metadata reads=$reads steps=$steps covers=$covers inline_bytes=$ib l0_substituted=$sub"
  echo "$d $reads" >> "$W/dist.txt"
  check "fetch at distance $d correct" sh -c "[ \"\$(git -C '$W/dist' rev-parse origin/main)\" = \"\$(git -C '$W/a' rev-parse HEAD)\" ] && git -C '$W/dist' fsck --full >/dev/null"
done
check "planning reads sub-linear (d=256 reads <= 40)" sh -c "awk '\$1==256 && \$2>40 {exit 1}' '$W/dist.txt'"
# packfile URIs (protocol v2 packfile-uris), stock client opting in
s0=$(curl -s "$URL_BASE/_stats")
check "clone with packfile-uris" sh -c "git -c fetch.uriprotocols=http clone -q '$U' '$W/uri' && git -C '$W/uri' fsck --full --strict"
s1=$(curl -s "$URL_BASE/_stats")
echo "# stats: $s1"
c0=$(echo "$s0" | grep -o '"bytes_out_cache":[0-9]*' | cut -d: -f2)
c1=$(echo "$s1" | grep -o '"bytes_out_cache":[0-9]*' | cut -d: -f2)
check "packs downloaded from cache service ($((c1 - c0)) bytes)" test "$c1" -gt "$c0"
line=$(grep '"ev":"fetch_served"' "$EV" | tail -1 | cut -c1-600)
echo "# $line"
check "uri clone matches" test "$(git -C "$W/uri" rev-parse HEAD)" = "$(git -C "$W/a" rev-parse HEAD)"
check "stock clone without uri opt-in still inline" sh -c "git clone -q '$U' '$W/inl' && git -C '$W/inl' fsck --full"
# fresh thin L0 pushes (not yet covered) are served by URI via their thick twins: empty inline pack
for i in 1 2 3; do echo "tail $i" >> "$W/a/data"; git -C "$W/a" commit -qam "tail$i"; git -C "$W/a" push -q origin main; done
sleep 1
check "uri clone right after thin pushes + fsck" sh -c "git -c fetch.uriprotocols=http clone -q '$U' '$W/uri2' && git -C '$W/uri2' fsck --full --strict"
line=$(grep '"ev":"fetch_served"' "$EV" | tail -1)
inl=$(echo "$line" | grep -o '"inline_objects":[0-9]*' | cut -d: -f2)
check "packfile-uri client gets an empty inline pack (inline_objects=$inl)" test "$inl" = 0
check "twins recorded" sh -c "grep -q '\"ev\":\"twin_committed\"' '$EV'"
