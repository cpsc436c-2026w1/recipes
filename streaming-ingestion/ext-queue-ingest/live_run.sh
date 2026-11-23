#!/bin/bash
# The live run of ext-queue-ingest, captured masked in .local/ (or $OUT; output/ holds the recorded run).
#   PREFIX=queue-yourname bash ext-queue-ingest/live_run.sh        # about 2 h 30 min
# From streaming-ingestion: a trail that counts the SQS calls, then the rush hour twice at once, at the default
# 30 s window ($PREFIX, BATCH_WINDOW_S=30; the default is 60 s) and without a window for comparison ($PREFIX-w0),
# each measured by latency.py, the table batching-window.txt beside it, the clean-up's list, the clean-up, and the
# listing that shows nothing is left.
# Run it from a laptop terminal (CloudShell ends idle sessions). Costs: README, Cost and clean-up.
set -uo pipefail
cd "$(dirname "$0")/.."
export PREFIX=${PREFIX:-recipe-queue} AWS_REGION=${AWS_REGION:-ca-central-1}
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
mask() { sed -E -e "s/$ACCT/111122223333/g" -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' \
             -e "s#$PWD/##g"; }
run_id() { grep -o 'run id [^ ,)]*' "$1" | tail -1 | cut -d' ' -f3; }
TMP=$(mktemp -d)
OUT=${OUT:-.local/ext-queue-ingest.txt}   # output/ holds the recorded run: a new run goes to .local/ unless OUT says so
DIR=$(dirname "$OUT"); mkdir -p "$DIR"
{
  echo "# ext-queue-ingest in AWS, $(date -u +%Y-%m-%dT%H:%MZ), PREFIX=$PREFIX and $PREFIX-w0"
  echo "== request_count.py up (a few minutes before the first send: logging starts late)"
  uv run python ext-queue-ingest/request_count.py up 2>&1
  sleep 600
  echo "== run.py --target aws --speed 1 --keep: 30 s window ($PREFIX) and --no-window ($PREFIX-w0), at once"
  BATCH_WINDOW_S=30 uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep --wait 1800 > "$TMP/w30.txt" 2>&1 &
  PREFIX=$PREFIX-w0 uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep --wait 1800 --no-window \
    > "$TMP/w0.txt" 2>&1 &
  wait
  cat "$TMP/w30.txt" "$TMP/w0.txt"
  sleep 900                                               # CloudWatch metrics and CloudTrail files arrive late
  uv run python ext-queue-ingest/request_count.py count --json "$TMP/requests.json" 2>&1 | grep trip-completed
  for p in "$PREFIX" "$PREFIX-w0"; do
    f=$TMP/w30.txt; [ "$p" = "$PREFIX" ] || f=$TMP/w0.txt
    echo "== latency.py ($p)"
    PREFIX=$p uv run python ext-queue-ingest/latency.py --run-id "$(run_id "$f")" --requests "$TMP/requests.json" \
      2>&1 | tail -4
  done
  uv run python ext-queue-ingest/latency.py --table "$(run_id "$TMP/w0.txt")" "$(run_id "$TMP/w30.txt")" \
    > "$DIR/batching-window.txt"
  cat "$DIR/batching-window.txt"
  echo "== replay_dlq.py --probe"
  uv run python ext-queue-ingest/replay_dlq.py --probe 2>&1
  for p in "$PREFIX-w0" "$PREFIX"; do
    echo "== clean_up.py (the list), then --yes ($p)"
    PREFIX=$p uv run python clean_up.py 2>&1 | grep -v '^absent'
    PREFIX=$p uv run python clean_up.py --yes 2>&1 | grep -v '^absent'
  done
} 2>&1 | mask > "$OUT"
mask < "$DIR/batching-window.txt" > "$TMP/t" && cp "$TMP/t" "$DIR/batching-window.txt"
echo "written $OUT ($(wc -l < "$OUT") lines) and $DIR/batching-window.txt; the sim/data stand-in is synthetic"
