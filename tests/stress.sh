#!/bin/sh
# Mixed stress: W writers x R rounds on distinct branches + hot trunk contention, concurrent
# readers cloning/fetching and fsck-ing the whole time. Any reader failure = closure bug.
. "$(dirname "$0")/lib.sh"
WR="${WR:-40}"; ROUNDS="${ROUNDS:-5}"; RD="${RD:-8}"
trap 'finish' EXIT
start_server
"$RUN" create-repo st
U="$URL_BASE/st.git"
git clone -q "$U" "$W/seed"; commit_file "$W/seed" base base base; git -C "$W/seed" push -q origin main
for i in $(seq 1 "$WR"); do cp -R "$W/seed" "$W/w$i"; git -C "$W/w$i" checkout -qb "s$i"; done
: > "$W/stop"; rm -f "$W/stop"
writer() {
  i=$1; d="$W/w$i"
  for r in $(seq 1 "$ROUNDS"); do
    printf '%s %s\n' "$i" "$r" >> "$d/f$i"; git -C "$d" add -A; git -C "$d" commit -qm "w$i r$r"
    git -C "$d" push -q origin "s$i" || echo "writer $i round $r push failed" >> "$W/errors"
    # every writer also races on trunk now and then (rejections expected, never corruption)
    if [ $((r % 2)) = 0 ]; then
      git -C "$d" fetch -q origin main && git -C "$d" merge -q --no-edit origin/main >/dev/null 2>&1
      git -C "$d" push -q origin "HEAD:main" >/dev/null 2>&1 || true
    fi
  done
}
reader() {
  j=$1; n=0
  while [ ! -f "$W/stop" ]; do
    n=$((n+1)); d="$W/r$j-$n"
    if ! git clone -q --mirror "$U" "$d" 2>"$d.err"; then echo "reader $j clone $n failed: $(tail -2 "$d.err")" >> "$W/errors"; continue; fi
    git -C "$d" fsck --full --strict >/dev/null 2>"$d.fsck" || echo "reader $j fsck $n failed: $(head -3 "$d.fsck")" >> "$W/errors"
    sleep 0.2
    git -C "$d" fetch -q origin '+refs/*:refs/*' 2>"$d.err2" || echo "reader $j fetch $n failed: $(tail -2 "$d.err2")" >> "$W/errors"
    git -C "$d" fsck --full >/dev/null 2>&1 || echo "reader $j fsck-after-fetch $n failed" >> "$W/errors"
    rm -rf "$d"
  done
}
for j in $(seq 1 "$RD"); do reader "$j" & done
t0=$(date +%s)
wpids=""
for i in $(seq 1 "$WR"); do writer "$i" & wpids="$wpids $!"; done
for p in $wpids; do wait "$p"; done
touch "$W/stop"; wait
echo "# $WR writers x $ROUNDS rounds with $RD readers in $(( $(date +%s) - t0 ))s"
echo "# $(curl -s "$URL_BASE/_stats")"
check "no reader/writer errors" sh -c "[ ! -s '$W/errors' ]"
[ -s "$W/errors" ] && head -20 "$W/errors"
for i in $(seq 1 "$WR"); do
  [ "$(git ls-remote "$U" "refs/heads/s$i" | cut -f1)" = "$(git -C "$W/w$i" rev-parse "s$i")" ] || echo "s$i mismatch" >> "$W/mismatch"
done
check "every writer's final push visible" sh -c "[ ! -s '$W/mismatch' ]"
check "final mirror clone + fsck" sh -c "git clone -q --mirror '$U' '$W/final.git' && git -C '$W/final.git' fsck --full --strict"
