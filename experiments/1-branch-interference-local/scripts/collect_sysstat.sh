#!/usr/bin/env bash
# Capture CPU / disk / process stats for a fixed duration, into data/sysstat/<tag>/.
# Usage: collect_sysstat.sh <tag> <duration_seconds>
set -euo pipefail
TAG="$1"
DUR="${2:-90}"
OUT="/mydata/jiyu/neon/experiments/1-branch-interference-local/data/sysstat/$TAG"
mkdir -p "$OUT"

mpstat -P ALL 1 "$DUR" > "$OUT/mpstat.txt" 2>&1 &
MP_PID=$!
iostat -x -d nvme1n1 nvme2n1 1 "$DUR" > "$OUT/iostat.txt" 2>&1 &
IO_PID=$!
pidstat -C "pageserver|postgres|pagebench|pgbench" 1 "$DUR" > "$OUT/pidstat.txt" 2>&1 &
PD_PID=$!

wait $MP_PID $IO_PID $PD_PID
