#!/bin/bash
# Pre-registered key-lock regression check (docs/AUDIT-NET-REMEDIATION.md): base, fix, base, fix; load-gated.
B="${REPO:?the repository checkout (base)}"; F=$B/.worktrees/fix; O="${OUT:?output directory}"   # paths are placeholders in the published copy
for i in 1 2; do for t in base fix; do
  [ $t = base ] && S=$B || S=$F
  while :; do LA=$(sysctl -n vm.loadavg | awk '{print $2}'); awk "BEGIN{exit !($LA < 16)}" && break; sleep 30; done
  echo "$t$i start $(uptime)" >> $O/log.txt
  SERVER_DIR=$S CARGO_HOME=$B/.cargo-home $F/verify/net/measure_run.sh $O/$t$i.json > $O/$t$i.txt 2>&1
  echo "$t$i end $(uptime) rc=$?" >> $O/log.txt
done; done
