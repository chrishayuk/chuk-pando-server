#!/usr/bin/env bash
# Run the independent suites from the experiment record against THIS server (unchanged suites; nothing copied in).
#   hidden CG1-CG4 (+review1, structural) from verify/heldout (copied verbatim from the retired experiment record) ;  frozen blind CG5/CG6 suite (commit 7229587) from $CG56 (kept privately; not in this repository)
# Usage: verify/run_suites.sh LABEL [hidden|cg56|par|all|public]   Results: verify/results/<UTC>-<LABEL>/
#   all     every suite, including the frozen blind CG5/CG6 suite (CG56=<dir>, kept privately), strict
#   public  every suite whose inputs are in this repository (all but cg56 and par), with the public audit-archive
#           package (G_ARCHIVE_CHECK=public); the gate checks exactly those suites and labels the verdict PARTIAL
# Required environment (docs/VERIFY.md): G_CLIENTS, FORK_GIT; CG56 for all/cg56/par; MinIO (G_MINIO) for several suites.
# Every suite writes a per-test record (JUnit XML; cargo output for unit tests). The exit status is the verdict of
# verify/gate/gate.py over those records (unexpected failures, unexpected skips, missing tests, count floors), never a
# suite's own exit status or summary line. 0 = gate passed, 1 = gate failed, 2 = inputs did not verify.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; REC="$HERE/verify"; CG56="${CG56:-}"
L="$1"; WHAT="${2:-all}"; OUT="$HERE/verify/results/$(date -u +%Y%m%dT%H%M%SZ)-$L"; mkdir -p "$OUT"
SRV="${SERVER_DIR:-$HERE}"
export G_CLIENTS="${G_CLIENTS:?set G_CLIENTS, e.g. stock=/path/to/git-2.55.0,apple=/usr/bin/git (docs/VERIFY.md)}"   # the Git clients the suites drive; the gate runs use a pinned stock Git 2.55.0 build
RUN="$SRV/server/run.sh"; export CARGO_HOME="$HERE/.cargo-home"
export G_STOCK_GIT="${G_STOCK_GIT:-$(echo "$G_CLIENTS" | tr , '\n' | sed -n 's/^stock=//p')}"
[ "$WHAT" = public ] && export G_ARCHIVE_CHECK=public
# wait for a quiet machine (1-minute load average below 16); macOS sysctl or Linux /proc/loadavg
while :; do LA=$( (sysctl -n vm.loadavg 2>/dev/null || cat /proc/loadavg) | tr -d '{}' | awk '{print $1}'); awk "BEGIN{exit !(${LA:-0} < 16)}" && break; sleep 60; done
{ echo "server_tree=$SRV server_commit=$(git -C "$SRV" rev-parse HEAD) dirty=$(git -C "$SRV" status --porcelain -- server | wc -l | tr -d ' ')"
  echo "heldout_tree=$(git -C "$HERE" rev-parse HEAD:verify/heldout) clients=$G_CLIENTS start=$(date -u +%FT%TZ)"; uptime; } > "$OUT/meta.txt"
# Build the tree under test first: run.sh only builds when the binary is missing, so a stale binary could be tested.
( cd "$SRV/server" && cargo build --release --offline -q ) || { echo "build failed"; exit 2; }
echo "binary_sha256=$(shasum -a 256 "$SRV/server/target/release/pando" | cut -d' ' -f1)" >> "$OUT/meta.txt"
( cd "$REC" && shasum -a 256 -c --quiet heldout/MANIFEST.sha256 ) || { echo "heldout manifest mismatch"; exit 2; }
case "$WHAT" in all|cg56|par)
  [ -n "$CG56" ] || { echo "CG56 is not set: the frozen blind CG5/CG6 suite is kept privately; use 'public' without it"; exit 2; }
  ( cd "$CG56/work" && grep -v "  ./results/" MANIFEST.sha256 | shasum -a 256 -c --quiet ) || { echo "cg56 suite mismatch"; exit 2; } ;;
esac
echo "mode=$WHAT archive_check=${G_ARCHIVE_CHECK:-full}" >> "$OUT/meta.txt"
if [ "$WHAT" = hidden ] || [ "$WHAT" = all ] || [ "$WHAT" = public ]; then
  ( cd "$REC" && G_RUN_SH="$RUN" G_MATRIX_OUT="$OUT/matrix.json" python3 -m pytest heldout/tests_cg1.py heldout/tests_cg2.py heldout/tests_cg3.py \
      heldout/tests_cg4.py heldout/tests_review1.py -v --tb=short -rfEs -p no:cacheprovider --junitxml="$OUT/hidden.xml" > "$OUT/hidden.txt" 2>&1 ); echo "hidden: $(tail -1 "$OUT/hidden.txt")"
fi
if [ "$WHAT" = cg56 ] || [ "$WHAT" = all ]; then
  ( cd "$CG56" && G_RUN_SH="$RUN" G_SEED=11 G_METRICS_OUT="$OUT/cg56-metrics.jsonl" work/run.sh tests_cg5.py tests_cg6.py \
      --junitxml="$OUT/cg56.xml" > "$OUT/cg56.txt" 2>&1 ); echo "cg56: $(tail -1 "$OUT/cg56.txt")"
fi
if [ "$WHAT" = par ] || [ "$WHAT" = all ]; then
  # Parallel URI download with the OpenAI fork client configured as its own docs require (fetch.packfileUriJobs > 1).
  ( cd "$CG56" && G_RUN_SH="$RUN" G_SEED=11 G_CG6_PAR_CONFIG='{"config": {"fetch.packfileUriJobs": "4", "protocol.version": "2"}}' \
      work/run.sh tests_cg6.py -k parallel --junitxml="$OUT/par.xml" > "$OUT/par.txt" 2>&1 ); echo "par: $(tail -1 "$OUT/par.txt")"
fi
if [ "$WHAT" = all ] || [ "$WHAT" = public ]; then
  # Unit tests, with a floor on how many ran (a test module was once deleted by an edit without anyone noticing)
  ( cd "$SRV/server" && cargo test --release --offline > "$OUT/unit.txt" 2>&1 )
  echo "unit: $(grep 'test result' "$OUT/unit.txt" | tail -1)"
  # Release smoke test (verify/release/smoke.py) of the tree's binary: serve, push, clone, explain, explore, port and
  # data locks, concurrent branches, hard kill and restart. The same script tests each packaged artifact in CI.
  if [ -f "$SRV/verify/release/smoke.py" ]; then
    python3 "$SRV/verify/release/smoke.py" --bin "$SRV/server/target/release/pando" --port 18733 --junit "$OUT/smoke.xml" > "$OUT/smoke.txt" 2>&1
    echo "smoke: $(tail -1 "$OUT/smoke.txt")"
  fi
  # Product regression tests (P0 durability/correctness, P1 fidelity incl. force-push cover rules), against the same tree
  ( SERVER_DIR="$SRV" "$HERE/verify/p0/run.sh" --junitxml="$OUT/p0.xml" > "$OUT/p0.txt" 2>&1 ); echo "p0: $(tail -1 "$OUT/p0.txt")"
  ( SERVER_DIR="$SRV" "$HERE/verify/p1/run.sh" --junitxml="$OUT/p1.xml" > "$OUT/p1.txt" 2>&1 ); echo "p1: $(tail -1 "$OUT/p1.txt")"
  # Distributed milestone (two frontends, one metadata service, one pack store), when the tree under test has it
  if [ -x "$SRV/verify/dist/run.sh" ]; then
    ( SERVER_DIR="$SRV" "$SRV/verify/dist/run.sh" --junitxml="$OUT/dist.xml" > "$OUT/dist.txt" 2>&1 ); echo "dist: $(tail -1 "$OUT/dist.txt")"
  fi
  # Network-capable deployment (TCP metadata + MinIO packs), when the tree has it and MinIO is installed
  if [ -x "$SRV/verify/net/run.sh" ] && [ -x "${G_MINIO:-/opt/homebrew/bin/minio}" ]; then
    ( SERVER_DIR="$SRV" G_DIST_MODE=tcp-s3 "$SRV/verify/dist/run.sh" --junitxml="$OUT/dist-tcp-s3.xml" > "$OUT/dist-tcp-s3.txt" 2>&1 ); echo "dist (tcp-s3): $(tail -1 "$OUT/dist-tcp-s3.txt")"
    ( SERVER_DIR="$SRV" "$SRV/verify/net/run.sh" --junitxml="$OUT/net.xml" > "$OUT/net.txt" 2>&1 ); echo "net: $(tail -1 "$OUT/net.txt")"
  fi
  # The independent re-audit's reproducers (archived unmodified in the experiment record), against this tree
  if [ -x "$SRV/verify/audit/run_repro.sh" ] && [ -x "${G_MINIO:-/opt/homebrew/bin/minio}" ]; then
    "$SRV/verify/audit/run_repro.sh" "$SRV" "$OUT/repro" > "$OUT/repro.txt" 2>&1; cp "$OUT/repro/repro.xml" "$OUT/repro.xml" 2>/dev/null
    echo "repro: $(grep -c 'no defect observed' "$OUT/repro/summary.txt") of $(wc -l < "$OUT/repro/summary.txt" | tr -d ' ') without their defect"
  fi
  # The remediation review's reproducers (R20-R22), same runner
  if [ -x "$SRV/verify/audit/run_repro.sh" ] && [ -x "${G_MINIO:-/opt/homebrew/bin/minio}" ]; then
    G_REPRO_SET=rev "$SRV/verify/audit/run_repro.sh" "$SRV" "$OUT/repro-rev" > "$OUT/repro-rev.txt" 2>&1; cp "$OUT/repro-rev/repro.xml" "$OUT/repro-rev.xml" 2>/dev/null
    echo "repro-rev: $(grep -c 'no defect observed' "$OUT/repro-rev/summary.txt") of $(wc -l < "$OUT/repro-rev/summary.txt" | tr -d ' ') without their defect"
  fi
  # Object-store fault injection through the test-only relay (stall, refuse, latency, trickle)
  if [ -x "$SRV/verify/fault/run.sh" ] && [ -x "${G_MINIO:-/opt/homebrew/bin/minio}" ]; then
    ( SERVER_DIR="$SRV" "$SRV/verify/fault/run.sh" --junitxml="$OUT/fault.xml" > "$OUT/fault.txt" 2>&1 ); echo "fault: $(tail -1 "$OUT/fault.txt")"
  fi
  # Client CLI and its read-only diagnostic boundary (pando status / explore / explain), when the tree has it
  if [ -x "$SRV/verify/cli/run.sh" ] && [ -x "${G_MINIO:-/opt/homebrew/bin/minio}" ]; then
    ( SERVER_DIR="$SRV" "$SRV/verify/cli/run.sh" --junitxml="$OUT/cli.xml" > "$OUT/cli.txt" 2>&1 ); echo "cli: $(tail -1 "$OUT/cli.txt")"
  fi
  # Off-host access control (Basic auth, signed packfile URIs, metadata handshake), when the tree has it
  if [ -x "$SRV/verify/fly/run.sh" ]; then
    ( SERVER_DIR="$SRV" "$SRV/verify/fly/run.sh" --junitxml="$OUT/fly.xml" > "$OUT/fly.txt" 2>&1 ); echo "fly: $(tail -1 "$OUT/fly.txt")"
  fi
fi
echo "$OUT"
case "$WHAT" in all) SUITES="${G_GATE_SUITES:-}" ;; public) SUITES="hidden,unit,smoke,p0,p1,dist,dist-tcp-s3,net,repro,fault,repro-rev,cli,fly" ;; *) SUITES="$WHAT" ;; esac
# Always strict, always tied to this run's records and binary (review V7). A narrowed check is labelled PARTIAL.
python3 "$HERE/verify/gate/gate.py" "$OUT" ${SUITES:+--suites=$SUITES} --strict --require-meta --tree="$SRV"
