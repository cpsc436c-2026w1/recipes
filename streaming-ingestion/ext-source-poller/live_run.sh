#!/bin/bash
# The live run of ext-source-poller, captured masked in .local/ext-source-poller.txt (or $OUT; output/ holds the
# recorded run).
#   PREFIX=recipe-poller AWS_PROFILE=... bash ext-source-poller/live_run.sh      # about 2 h 30 min (8 slots)
# Runs from streaming-ingestion: the main stream and consumer (kept), the poller and its schedule, 8 slots of the feed
# published in real time, the latency, stop, the clean-up. Run it from a laptop terminal (CloudShell ends idle sessions).
set -uo pipefail
cd "$(dirname "$0")/.."
export PREFIX=${PREFIX:-recipe-poller} AWS_REGION=${AWS_REGION:-ca-central-1}
SLOTS=${SLOTS:-8}
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
mask() { sed -E -e "s/$ACCT/111122223333/g" -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' \
             -e "s#$PWD/##g"; }
OUT=${OUT:-.local/ext-source-poller.txt}   # output/ holds the recorded run: a new run goes to .local/ unless OUT says so
mkdir -p "$(dirname "$OUT")"
{
  echo "# ext-source-poller in AWS, $(date -u +%Y-%m-%dT%H:%MZ), PREFIX=$PREFIX, $SLOTS slots"
  echo "== main/run.py --target aws --keep"
  uv run python main/run.py --target aws --keep 2>&1 | tail -3
  echo "== ext-source-poller/run.py --target aws"
  uv run python ext-source-poller/run.py --target aws 2>&1
  echo "== feed.py --to aws --live --slots $SLOTS"
  uv run python ext-source-poller/feed.py --to aws --live --slots "$SLOTS" 2>&1 | tee /tmp/feed.txt
  sleep 1200                                           # the next two polls take the last files
  echo "== latency.py"
  uv run python ext-source-poller/latency.py --published "$(grep -o 'evidence/feed-[^/]*/published.jsonl' /tmp/feed.txt)" 2>&1
  echo "== run.py stop; clean_up.py --dry-run, then --yes"
  uv run python ext-source-poller/run.py stop 2>&1
  uv run python clean_up.py 2>&1 | grep -v '^absent'
  uv run python clean_up.py --yes 2>&1 | grep -v '^absent'
  uv run python scripts/lake.py down 2>&1
  uv run python scripts/lake.py down --yes 2>&1
} | mask > "$OUT"
echo "written $OUT ($(wc -l < "$OUT") lines); the feed day is synthetic (sim/stand_in.py)"
