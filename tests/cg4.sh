#!/bin/sh
# CG4: N concurrent writers on N distinct branches; same-ref conflicts; RYOW; monotonic reads.
. "$(dirname "$0")/lib.sh"
N="${N:-200}"
P="${P:-100}"
trap 'finish' EXIT
start_server
"$RUN" create-repo r4
U="$URL_BASE/r4.git"
git clone -q "$U" "$W/a"
commit_file "$W/a" base base base
git -C "$W/a" push -q origin main
# one commit per branch (built locally, fast)
base=$(git -C "$W/a" rev-parse HEAD)
tree=$(git -C "$W/a" rev-parse HEAD^{tree})
for i in $(seq 1 "$N"); do
  blob=$(printf 'branch %s\n' "$i" | git -C "$W/a" hash-object -w --stdin)
  t=$(printf '100644 blob %s\tfile%s\n' "$blob" "$i" | git -C "$W/a" mktree)
  c=$(git -C "$W/a" commit-tree "$t" -p "$base" -m "b$i")
  git -C "$W/a" update-ref "refs/heads/b$i" "$c"
done
s0=$(curl -s "$URL_BASE/_stats")
cat > "$W/push1.sh" <<EOF
#!/bin/sh
i=\$1
cd "$W/a" || exit 1
git push -q origin "b\$i" >/dev/null 2>"$W/err.\$i" || { echo "push b\$i failed"; exit 1; }
# read-your-own-write: immediately visible to the same client
t=\$(git ls-remote origin "refs/heads/b\$i" | cut -f1)
[ "\$t" = "\$(git rev-parse refs/heads/b\$i)" ] || { echo "RYOW violated for b\$i"; exit 1; }
EOF
chmod +x "$W/push1.sh"
t0=$(date +%s)
seq 1 "$N" | xargs -P "$P" -n 1 "$W/push1.sh" > "$W/cg4.out" 2>&1
rc=$?
t1=$(date +%s)
s1=$(curl -s "$URL_BASE/_stats")
echo "# $N pushes with parallelism $P in $((t1-t0))s"
echo "# stats before: $s0"
echo "# stats after:  $s1"
check "all $N concurrent pushes to distinct branches succeeded with RYOW" sh -c "[ $rc = 0 ] && ! grep -q . '$W/cg4.out'"
cat "$W/cg4.out" | head -5
cnt=$(git ls-remote "$U" 'refs/heads/b*' | wc -l | tr -d ' ')
check "all $N branches present ($cnt)" test "$cnt" = "$N"
conf=$(echo "$s1" | sed 's/.*"metadata_conflicts":\([0-9]*\).*/\1/')
check "no metadata transaction conflicts between distinct branches (conflicts=$conf)" test "$conf" = 0
check "mirror clone + fsck of all branches" sh -c "git clone -q --mirror '$U' '$W/m.git' && git -C '$W/m.git' fsck --full --strict"

# same-ref conflicts under contention: K writers race on one branch, repeatedly
K=8
rm -rf "$W/hot"; mkdir "$W/hot"
for j in $(seq 1 $K); do git clone -q "$U" "$W/hot/$j"; done
race_round() {
  for j in $(seq 1 $K); do (cd "$W/hot/$j" && git fetch -q && git reset -q --hard origin/main && commit_file . hot "$1-$j" "hot $1 $j" && git push -q origin main >/dev/null 2>&1 && echo "$j $(git rev-parse HEAD)" >> "$W/hotwin.$1") & done
  wait
  [ "$(wc -l < "$W/hotwin.$1" | tr -d ' ')" = 1 ] && [ "$(git ls-remote "$U" refs/heads/main | cut -f1)" = "$(cut -d' ' -f2 "$W/hotwin.$1")" ]
}
for r in 1 2 3 4; do check "hot ref round $r: exactly one of $K racing writers wins" race_round $r; done

# monotonic reads: a reader polling a ref while a writer advances it
git clone -q "$U" "$W/mono"; git -C "$W/mono" checkout -qb mono
: > "$W/mono.order"
for i in $(seq 1 15); do commit_file "$W/mono" m "$i" "m$i"; git -C "$W/mono" rev-parse HEAD >> "$W/mono.order"; done
git -C "$W/mono" push -q origin "$(sed -n 1p "$W/mono.order"):refs/heads/mono"
( for i in $(seq 2 15); do git -C "$W/mono" push -q origin "$(sed -n ${i}p "$W/mono.order"):refs/heads/mono"; done; touch "$W/mono.done" ) &
: > "$W/mono.seen"
while [ ! -f "$W/mono.done" ]; do git ls-remote "$U" refs/heads/mono | cut -f1 >> "$W/mono.seen"; done
wait
mono_ok() {
  last=0
  while read -r t; do
    n=$(grep -n "^$t\$" "$W/mono.order" | cut -d: -f1)
    [ -n "$n" ] || return 1
    [ "$n" -ge "$last" ] || { echo "went backwards $last -> $n"; return 1; }
    last=$n
  done < "$W/mono.seen"
}
check "ref never goes backwards for a polling reader ($(wc -l < "$W/mono.seen" | tr -d ' ') reads)" mono_ok
