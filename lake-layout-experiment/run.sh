#!/usr/bin/env bash
# The live run of the lake layout experiment, in order. DRY=1 prints each command and runs nothing.
#   PREFIX=layout-yourname MAX_USD=2.00 DRY=1 bash run.sh
#   PREFIX=layout-yourname MAX_USD=2.00 bash run.sh      # AWS credentials for ca-central-1
# (and COURSE_CATALOG_ID=YOUR_COURSE_CATALOG_ID when the Athena data source `course` is missing). The build picks its
# plan with MAX_USD, so set it before the build. STEP=build stops after the build, so you can read the probe and the
# bound (results.duckdb, table growth) before the 450 runs; STEP=run goes on from there (it does not build again).
# Everything printed is added to output/live.txt with the account number masked (STEP=build starts the file, STEP=run
# adds to it). It ends with experiment.py clean (the list), then clean --yes; scripts/lake.py down comes after every
# recipe's clean-up.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ca-central-1

run() {
  echo "+ $*"
  if [ -z "${DRY:-}" ]; then "$@"; fi
}

main() {
  if [ "${STEP:-}" != "run" ]; then run uv run python experiment.py build; fi
  if [ "${STEP:-}" = "build" ]; then return; fi
  run uv run python experiment.py run
  run uv run python experiment.py summary
  run env LIVE=1 uv run behave -t @live       # before late: it counts DS01's rows in the four tables of 1 copy
  run uv run python experiment.py late
  run uv run python experiment.py clean          # the plan, printed before anything is deleted
  run uv run python experiment.py clean --yes
}

if [ -n "${DRY:-}" ]; then
  main
else
  if [ "${STEP:-}" != "run" ]; then : > output/live.txt; fi
  main 2>&1 | sed -E 's/[0-9]{12}/111122223333/g' | tee -a output/live.txt
fi
