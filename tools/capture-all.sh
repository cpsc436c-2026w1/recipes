#!/bin/bash
# Fresh capture of the bash recipes, in dependency order, starting from a clean account:
#   clean up -> dynamodb main and extensions (kept) -> lambda-dynamodb main, extensions, clean-up -> dynamodb clean-up.
#   AWS_PROFILE=... BUCKET=<bucket> bash tools/capture-all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${BUCKET:?set BUCKET (dynamodb ext-nightly-report exports to it)}"
export BUCKET
echo "== clean start (outputs of this step are not kept)"
bash lambda-dynamodb/clean_up.sh >/dev/null 2>&1 || true
bash dynamodb/clean_up.sh >/dev/null 2>&1 || true
for t in trips rider-trips drivers; do aws dynamodb wait table-not-exists --table-name "${PREFIX:-recipe}-$t" 2>/dev/null || true; done
bash tools/capture.sh dynamodb main ext-riders-and-drivers ext-nightly-report
bash tools/capture.sh lambda-dynamodb main ext-redeploy ext-metrics clean-up
bash tools/capture.sh dynamodb clean-up
