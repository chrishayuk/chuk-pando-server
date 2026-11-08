#!/usr/bin/env bash
# Remove every Fly resource the Pando experiment creates, then VERIFY that nothing billable remains
# (docs/FLY-DEPLOYMENT-PROPOSAL.md §4). Order:
#   1. empty the Tigris bucket (objects are removed with the bucket's own credentials, saved privately at creation
#      in ~/.pando-fly/storage-create.out), then destroy it;
#   2. destroy the metadata volume explicitly (its snapshots are listed first and reported);
#   3. destroy the apps (with their Machines, IPs and secrets);
#   4. verify: no pando app, Machine, volume or bucket left. Exit 1 if anything remains.
# Usage: deploy/fly/teardown.sh [--yes]
set -u
APPS="pando-git pando-meta pando-load"; BUCKET="pando-packs"; S="$HOME/.pando-fly"; LOG="$S/teardown.log"
umask 077; mkdir -p "$S"
say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
echo "Will destroy: apps [$APPS] (if present), the metadata volume, and Tigris bucket [$BUCKET] with all its objects."
[ "${1:-}" = "--yes" ] || { read -r -p "Type 'destroy' to continue: " a; [ "$a" = destroy ] || exit 1; }

# 1. bucket: empty, then destroy
if fly storage list 2>/dev/null | grep -qw "$BUCKET"; then
  ak=$(grep -E '^ *AWS_ACCESS_KEY_ID' "$S/storage-create.out" 2>/dev/null | head -1 | sed 's/.*[:=] *//')
  sk=$(grep -E '^ *AWS_SECRET_ACCESS_KEY' "$S/storage-create.out" 2>/dev/null | head -1 | sed 's/.*[:=] *//')
  ep=$(grep -E '^ *AWS_ENDPOINT_URL_S3' "$S/storage-create.out" 2>/dev/null | head -1 | sed 's/.*[:=] *//'); ep="${ep:-https://fly.storage.tigris.dev}"
  if [ -n "$ak" ] && [ -n "$sk" ]; then
    n=0
    while :; do
      keys=$(curl -s --aws-sigv4 "aws:amz:auto:s3" --user "$ak:$sk" "$ep/$BUCKET?list-type=2&max-keys=1000" | grep -o '<Key>[^<]*</Key>' | sed 's/<[^>]*>//g')
      [ -n "$keys" ] || break
      while IFS= read -r k; do
        curl -s -o /dev/null --aws-sigv4 "aws:amz:auto:s3" --user "$ak:$sk" -X DELETE "$ep/$BUCKET/$k" && n=$((n + 1))
      done <<< "$keys"
    done
    say "bucket emptied: $n object(s) deleted"
  else
    say "no saved bucket credentials: relying on 'fly storage destroy' to remove a non-empty bucket"
  fi
  fly storage destroy "$BUCKET" --yes >> "$LOG" 2>&1 && say "bucket destroyed" || say "bucket destroy FAILED (see $LOG)"
fi

# 2. volume: report snapshots, destroy explicitly
for v in $(fly volumes list -a pando-meta --json 2>/dev/null | grep -o '"id": *"vol_[a-z0-9]*"' | grep -o 'vol_[a-z0-9]*'); do
  snaps=$(fly volumes snapshots list "$v" 2>/dev/null | grep -c '^ *vs_' || true)
  say "volume $v: $snaps snapshot(s) at teardown"
  fly volumes destroy "$v" --yes >> "$LOG" 2>&1 && say "volume $v destroyed" || say "volume $v destroy FAILED"
done

# 3. apps
for a in $APPS; do
  if fly status -a "$a" >/dev/null 2>&1; then fly apps destroy "$a" --yes >> "$LOG" 2>&1 && say "app $a destroyed" || say "app $a destroy FAILED"; fi
done

# 4. verify
left=0
for a in $APPS; do
  if fly apps list 2>/dev/null | grep -qE "(^| )$a( |$)"; then say "REMAINS: app $a"; left=1; fi
done
if fly storage list 2>/dev/null | grep -qw "$BUCKET"; then say "REMAINS: bucket $BUCKET"; left=1; fi
if [ $left -eq 0 ]; then
  say "verified: no pando app, Machine, volume or bucket remains. Volume snapshots of a destroyed volume may be kept by Fly for a few days and billed at \$0.08/GB-month (first 10 GB free) until expiry: see the Fly dashboard's billing page."
else
  say "TEARDOWN INCOMPLETE: the resources above are still billable"; exit 1
fi
