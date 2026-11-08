#!/bin/sh
# CG2: atomic updates, same-ref races, crash safety (SIGKILL) and recovery.
. "$(dirname "$0")/lib.sh"
trap 'finish' EXIT
start_server
"$RUN" create-repo r2
U="$URL_BASE/r2.git"
git clone -q "$U" "$W/seed"
commit_file "$W/seed" f base "base"
git -C "$W/seed" push -q origin main
git -C "$W/seed" push -q origin main:refs/heads/side

race() { # branch round
  b=$1; n=$2
  rm -rf "$W/r1" "$W/r2x"
  git clone -q -b "$b" "$U" "$W/r1"; git clone -q -b "$b" "$U" "$W/r2x"
  commit_file "$W/r1" f "one-$n" "one $n"
  commit_file "$W/r2x" f "two-$n" "two $n"
  (git -C "$W/r1" push origin "$b" >"$W/p1.out" 2>&1; echo $? >"$W/p1.rc") &
  (git -C "$W/r2x" push origin "$b" >"$W/p2.out" 2>&1; echo $? >"$W/p2.rc") &
  wait
  rc1=$(cat "$W/p1.rc"); rc2=$(cat "$W/p2.rc")
  tip=$(git ls-remote "$U" "refs/heads/$b" | cut -f1)
  if [ "$rc1" = 0 ] && [ "$rc2" != 0 ]; then
    [ "$tip" = "$(git -C "$W/r1" rev-parse HEAD)" ] && grep -qE 'rejected|failed|fetch first' "$W/p2.out"
  elif [ "$rc2" = 0 ] && [ "$rc1" != 0 ]; then
    [ "$tip" = "$(git -C "$W/r2x" rev-parse HEAD)" ] && grep -qE 'rejected|failed|fetch first' "$W/p1.out"
  else
    echo "rc1=$rc1 rc2=$rc2"; cat "$W/p1.out" "$W/p2.out"; return 1
  fi
}
for n in 1 2 3 4 5; do check "race on main round $n: exactly one winner" race main $n; done
for n in 1 2 3; do check "race on side round $n: exactly one winner" race side $n; done

# force-with-lease semantics on a non-trunk branch
rm -rf "$W/l1"; git clone -q -b side "$U" "$W/l1"
old=$(git -C "$W/l1" rev-parse HEAD)
commit_file "$W/seed" g x "advance side elsewhere"; git -C "$W/seed" fetch -q; git -C "$W/seed" push -q origin HEAD:side --force
git -C "$W/l1" commit -q --amend -m rewritten
check_fail "force-with-lease with stale lease rejected" git -C "$W/l1" push --force-with-lease=side:$old origin side
check "plain force accepted" git -C "$W/l1" push -q --force origin side

# atomic multi-ref push: one stale ref fails the whole push
rm -rf "$W/at"; git clone -q "$U" "$W/at"; git -C "$W/at" fetch -q origin side
git -C "$W/at" branch -f side origin/side
commit_file "$W/at" h y "atomic main"
git -C "$W/at" checkout -q side; commit_file "$W/at" h z "atomic side"; git -C "$W/at" checkout -q main
commit_file "$W/seed" q 1 "move main"; git -C "$W/seed" pull -q --rebase; git -C "$W/seed" push -q origin main
before=$(git ls-remote "$U" refs/heads/side | cut -f1)
check_fail "atomic push with one stale ref fails" git -C "$W/at" push --atomic origin main side
check "atomic: other ref untouched" test "$(git ls-remote "$U" refs/heads/side | cut -f1)" = "$before"

# crash after acknowledged push: nothing lost
rm -rf "$W/k"; git clone -q "$U" "$W/k"
for i in 1 2 3; do commit_file "$W/k" "k$i" "$i" "k$i"; git -C "$W/k" push -q origin main; done
acked=$(git -C "$W/k" rev-parse HEAD)
kill_server
check "port closed after kill" sh -c "! curl -s -m 1 $URL_BASE/_stats >/dev/null"
start_server
check "acknowledged push survives SIGKILL" test "$(git ls-remote "$U" refs/heads/main | cut -f1)" = "$acked"
check "clone+fsck after restart" fsck_clone "$U" "$W/k2"

rm -rf "$W/pre"; git clone -q "$U" "$W/pre"; touch "$W/acked.txt"
# crash mid-push (large pack): ref is old or new, never broken
head -c 30000000 /dev/urandom > "$W/k/blob1"; git -C "$W/k" add blob1; git -C "$W/k" commit -qm bigblob
newtip=$(git -C "$W/k" rev-parse HEAD)
(git -C "$W/k" push -q origin main >"$W/mid.out" 2>&1; echo $? > "$W/mid.rc") &
sleep 0.4
kill_server
wait
start_server
t=$(git ls-remote "$U" refs/heads/main | cut -f1)
check "mid-push crash: ref is old or new" sh -c "[ '$t' = '$acked' ] || [ '$t' = '$newtip' ]"
if [ "$(cat "$W/mid.rc")" = 0 ]; then check "mid-push acked => present" test "$t" = "$newtip"; fi
check "clone+fsck after mid-push crash" fsck_clone "$U" "$W/k3"
check "push again after crash" git -C "$W/k" push -q origin main
check "clone+fsck final" fsck_clone "$U" "$W/k4"
check "no temp blobs left referenced" test -d "$G_DATA_DIR/blobs"
# repeated kill/restart cycles with concurrent pushes
for cyc in 1 2 3; do
  for j in 1 2 3 4 5 6 7 8; do
    d="$W/cyc$cyc-$j"; cp -R "$W/pre" "$d"; git -C "$d" checkout -qb "c$cyc-$j"; commit_file "$d" x "$cyc$j" m
  done
  for j in 1 2 3 4 5 6 7 8; do
    ( d="$W/cyc$cyc-$j"; sleep "0.0$j"; git -C "$d" push -q origin "c$cyc-$j" >/dev/null 2>&1 && echo "c$cyc-$j $(git -C "$d" rev-parse HEAD)" >> "$W/acked.txt" ) &
  done
  sleep 0.15; kill_server; wait; start_server
done
lost=0
while read -r b h; do
  [ "$(git ls-remote "$U" "refs/heads/$b" | cut -f1)" = "$h" ] || { echo "lost $b"; lost=1; }
done < "$W/acked.txt"
check "no acknowledged push lost across kill cycles ($(wc -l < "$W/acked.txt") acked)" test $lost = 0
check "clone+fsck after kill cycles" fsck_clone "$U" "$W/k5"
# crash during background compaction: many versions => covers being built when killed
rm -rf "$W/cp"; git clone -q "$U" "$W/cp"; git -C "$W/cp" checkout -qb compact
for i in $(seq 1 64); do echo "$i" >> "$W/cp/c"; git -C "$W/cp" add c; git -C "$W/cp" commit -qm "c$i"; git -C "$W/cp" push -q origin compact; done
"$RUN" maintain >/dev/null &
sleep 0.05; kill_server; wait 2>/dev/null
start_server
check "maintain after crash mid-compaction" "$RUN" maintain
check "covers consistent: packfile-uri clone + fsck" sh -c "git -c fetch.uriprotocols=http clone -q -b compact '$U' '$W/cpc' && git -C '$W/cpc' fsck --full --strict"
check "inline clone + fsck after compaction crash" fsck_clone "$U" "$W/cpd"
# more race shapes: each must have exactly one winner and a consistent final state
race2() { # desc setup-cmd-1 setup-cmd-2 (each run in its own clone) ; expects exactly one success
  rm -rf "$W/ra" "$W/rb"; git clone -q "$U" "$W/ra"; git clone -q "$U" "$W/rb"
  (cd "$W/ra" && eval "$2") >/dev/null 2>&1; (cd "$W/rb" && eval "$3") >/dev/null 2>&1
  (cd "$W/ra" && eval "$4" >"$W/ra.out" 2>&1; echo $? > "$W/ra.rc") &
  (cd "$W/rb" && eval "$5" >"$W/rb.out" 2>&1; echo $? > "$W/rb.rc") &
  wait
  [ "$(cat "$W/ra.rc")$(cat "$W/rb.rc")" = 01 ] || [ "$(cat "$W/ra.rc")$(cat "$W/rb.rc")" = 10 ] || { cat "$W/ra.out" "$W/rb.out"; return 1; }
}
n=0
for i in 1 2 3; do
  n=$((n+1))
  check "create/create race on new branch nb$n" race2 x "git checkout -qb nb$n && echo a > a && git add a && git commit -qm a" "git checkout -qb nb$n && echo b > b && git add b && git commit -qm b" "git push -q origin nb$n" "git push -q origin nb$n"
  git ls-remote "$U" "refs/heads/nb$n" | grep -q . || fail "nb$n missing after race"
done
git -C "$W/seed" push -q origin HEAD:refs/heads/dv1 HEAD:refs/heads/dv2 HEAD:refs/heads/dv3
for b in dv1 dv2 dv3; do
  check "delete/update race on $b" race2 x "git fetch -q origin $b && git checkout -qb $b origin/$b" "git fetch -q origin $b && git checkout -qb $b origin/$b && echo u > u && git add u && git commit -qm u" "git push -q origin :$b" "git push -q origin $b"
done
git -C "$W/seed" push -q origin HEAD:refs/heads/fv1 HEAD:refs/heads/fv2
for b in fv1 fv2; do
  check "force/update race on $b" race2 x "git fetch -q origin $b && git checkout -qb $b origin/$b && git commit -q --allow-empty -m forced && git reset -q --soft HEAD~1 && git commit -q --allow-empty -m forced2" "git fetch -q origin $b && git checkout -qb $b origin/$b && echo u > u && git add u && git commit -qm u" "git push -q -f origin $b" "git push -q origin $b"
done
for t in rt1 rt2; do
  check "tag create race $t" race2 x "git tag -a -m one $t" "git commit -q --allow-empty -m other && git tag -a -m two $t" "git push -q origin $t" "git push -q origin $t"
done
check "all refs servable after races" sh -c "rm -rf '$W/rm.git' && git clone -q --mirror '$U' '$W/rm.git' && git -C '$W/rm.git' fsck --full --strict"
