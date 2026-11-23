#!/bin/bash
# Tear everything down: inventory, each clean-up step, the leftovers, then each service asked again.
#   PREFIX=cost-yourname bash scripts/teardown.sh          # lists what it would delete; deletes nothing
#   PREFIX=cost-yourname bash scripts/teardown.sh --yes    # deletes; exit 1 if anything is still there
export PYTHONUNBUFFERED=1   # the steps' own output keeps its place in the log
cd "$(dirname "$0")/.." && exec uv run python scripts/teardown.py "$@"
