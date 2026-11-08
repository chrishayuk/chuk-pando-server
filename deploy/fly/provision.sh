#!/usr/bin/env bash
# Create the Pando Fly.io experiment (docs/FLY-DEPLOYMENT-PROPOSAL.md, approved 2026-11-07 with a $30 ceiling).
# Secrets are generated locally into ~/.pando-fly/secrets.env (mode 600, outside the repository) and imported through
# stdin, never on a command line. Tigris credentials are set on pando-git by `fly storage create` (its output, which
# prints them, goes only to the same private directory). Teardown: deploy/fly/teardown.sh.
# Usage (from the repository root): deploy/fly/provision.sh
set -euo pipefail
ORG=personal; REGION=lhr; S="$HOME/.pando-fly"; LOG="$S/provision.log"
umask 077; mkdir -p "$S"
say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
[ -z "$(fly apps list 2>/dev/null | grep -E '^ ?pando-(git|meta|load) ')" ] || { echo "pando apps already exist; run teardown first"; exit 1; }
if [ ! -f "$S/secrets.env" ]; then
  r() { openssl rand -hex 32; }
  { echo "PANDO_META_SECRET=$(r)"; echo "PANDO_URI_SECRET=$(r)"; echo "PANDO_AUTH=pando:$(openssl rand -hex 16)"; } > "$S/secrets.env"
fi
say "creating apps in $ORG"
fly apps create pando-meta -o "$ORG" >> "$LOG" 2>&1
fly apps create pando-git -o "$ORG" >> "$LOG" 2>&1
say "volume pando_meta (1 GB, $REGION)"
fly volumes create pando_meta -a pando-meta -r "$REGION" -s 1 --yes >> "$LOG" 2>&1
say "Tigris bucket pando-packs (credentials set as secrets on pando-git)"
fly storage create -a pando-git -n pando-packs -o "$ORG" -y > "$S/storage-create.out" 2>&1
say "secrets (names only): pando-meta: PANDO_META_SECRET; pando-git: PANDO_META_SECRET PANDO_URI_SECRET PANDO_AUTH"
grep '^PANDO_META_SECRET=' "$S/secrets.env" | fly secrets import -a pando-meta --stage >> "$LOG" 2>&1
fly secrets import -a pando-git --stage < "$S/secrets.env" >> "$LOG" 2>&1
say "deploy pando-meta (one Machine, no public service)"
fly deploy . -c deploy/fly/meta.fly.toml --remote-only --ha=false --yes >> "$LOG" 2>&1
say "public IPs of pando-meta (must be none):"; fly ips list -a pando-meta 2>&1 | tee -a "$LOG"
say "deploy pando-git (two Machines)"
fly deploy . -c deploy/fly/git.fly.toml --remote-only --yes >> "$LOG" 2>&1
fly scale count 2 -a pando-git -r "$REGION" --yes >> "$LOG" 2>&1 || true
fly machine list -a pando-git 2>&1 | tee -a "$LOG"
say "done. Git: https://pando-git.fly.dev/<repo> with the credentials in $S/secrets.env (PANDO_AUTH)"
