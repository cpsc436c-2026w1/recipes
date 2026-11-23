#!/usr/bin/env bash
# The live run of ext-athena-engine, in order. DRY=1 prints each command and runs nothing.
#   PREFIX=recipe-yourname DRY=1 bash ext-athena-engine/run.sh
#   PREFIX=recipe-yourname bash ext-athena-engine/run.sh       # needs AWS credentials, COURSE_GATEWAY_URL, KB_MCP_TOKEN
# What it prints is saved in output/ext-athena-engine-live.txt with the account number masked. Clean up after:
#   uv run python ext-athena-engine/clean_up.py       (the plan), then --yes
#   uv run python clean_up.py                         (dashboard-chat's: the plan), then --yes
#   uv run python scripts/lake.py down                (the plan), then --yes
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=ca-central-1
OUT=output/ext-athena-engine-live.txt

run() {
  echo "+ $*"
  if [ -z "${DRY:-}" ]; then "$@"; fi
}

main() {
  run uv run python ext-athena-engine/deploy.py --data sim01
  run env LIVE=1 uv run behave features/athena_engine.feature -t @live -t @R130 -n "The same question in AWS"
  run env LIVE=1 uv run behave features/athena_engine.feature -t @live -t @R170
  run uv run python ext-athena-engine/clean_up.py --yes                 # SIM01 out, DS01 in (same table names)
  run uv run python ext-athena-engine/deploy.py --data ds01
  run env LIVE=1 uv run behave features/athena_engine.feature -t @live -t @R150
  run uv run python ext-athena-engine/run_athena.py --target aws --measure
  run uv run python ext-athena-engine/run_athena.py --target aws --eval
  run uv run python ext-athena-engine/clean_up.py --yes
}

if [ -n "${DRY:-}" ]; then
  main
else
  main 2>&1 | sed -E 's/[0-9]{12}/111122223333/g' | tee "$OUT"
fi
