#!/bin/bash
# The live run of cost-and-teardown in your lease (ca-central-1), captured with the account masked:
#   PREFIX=cost-yourname bash scripts/live_run.sh       # writes .local/main.txt (or $OUT); ends with teardown.sh --yes
# probe the inventory's list calls, create the demonstration system, one run of SIM01, its units and cost, the
# inventory, then the teardown and its proof. One run costs about 0.02 USD; the stream bills 0.0165 USD an hour until
# the teardown has run (README, Cost and clean-up).
set -uo pipefail
cd "$(dirname "$0")/.."
: "${PREFIX:?set PREFIX to a short lowercase prefix no other resource in your lease uses}"
export TEAM=${TEAM:-$PREFIX}
RUN=${RUN_ID:-run-$(date -u +%Y%m%dT%H%M%SZ)}
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
mask() { sed -E -e "s/$ACCT/111122223333/g" -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' \
             -e "s#$PWD/##g"; }
OUT=${OUT:-.local/main.txt}   # output/ holds the recorded run: a new run goes to .local/ unless OUT says so
mkdir -p "$(dirname "$OUT")"
{
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "\$ scripts/inventory.py --probe";  uv run python scripts/inventory.py --probe; echo "exit $?"
  echo "\$ scripts/demo_up.py";            uv run python scripts/demo_up.py; echo "exit $?"
  echo "\$ scripts/demo_run.py --run-id $RUN"; uv run python scripts/demo_run.py --run-id "$RUN"; echo "exit $?"
  echo "\$ scripts/units.py --run-id $RUN"; uv run python scripts/units.py --run-id "$RUN" --since "$START"; echo "exit $?"
  echo "\$ scripts/cost.py --run-id $RUN";  uv run python scripts/cost.py --run-id "$RUN"; echo "exit $?"
  echo "\$ scripts/inventory.py";          uv run python scripts/inventory.py; echo "exit $?"; cat docs/resources.md
  echo "\$ scripts/teardown.sh";           bash scripts/teardown.sh | tail -1
  echo "\$ scripts/teardown.sh --yes";     bash scripts/teardown.sh --yes; echo "exit $?"; cat docs/resources.md
} 2>&1 | mask | tee "$OUT"
