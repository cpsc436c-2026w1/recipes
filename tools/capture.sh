#!/bin/bash
# Run scenarios of one bash recipe and save each run's output as <recipe>/output/<scenario>.txt, masked: the account
# number, the bucket name, the id of a function URL or an API (<url-id>), the id of a serverless collection
# (<collection-id>), the role and session name of the caller (<role>), and the path of this clone.
# tools/test_outputs.py checks the result. For maintainers; students run the scenarios one by one.
#   bash tools/capture.sh dynamodb main ext-riders-and-drivers ext-nightly-report clean-up   (BUCKET=... for the export)
#   bash tools/capture.sh lambda-dynamodb main ext-redeploy ext-metrics clean-up   (needs the dynamodb tables, kept)
# A scenario is main (main/run.sh), ext-<name> (ext-<name>/run.sh) or clean-up (clean_up.sh). Scenarios run with
# KEEP=1, so the next one finds what the previous one made; clean-up removes it.
set -uo pipefail
cd "$(dirname "$0")/.."
R=${1:?recipe folder, e.g. dynamodb}; shift
[ $# -gt 0 ] || { echo "name the scenarios: main, ext-<name>, clean-up"; exit 1; }
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
mask() { sed -E -e "s/$ACCT/111122223333/g" ${BUCKET:+-e "s/${BUCKET//$ACCT/111122223333}/my-bucket-111122223333/g"} \
             ${BUCKET:+-e "s/$BUCKET/my-bucket-111122223333/g"} \
             -e 's#[a-z0-9]+\.(execute-api|lambda-url)\.([a-z0-9-]+)\.(amazonaws\.com|on\.aws)#<url-id>.\1.\2.\3#g' \
             -e 's#[a-z0-9]+\.aoss\.([a-z0-9-]+)\.on\.aws#<collection-id>.aoss.\1.on.aws#g' \
             -e 's#[a-z0-9]+\.([a-z]{2}(-[a-z]+)+-[0-9])\.aoss\.amazonaws\.com#<collection-id>.\1.aoss.amazonaws.com#g' \
             -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' \
             -e "s#$PWD/##g"; }
mkdir -p "$R/output"
for sc in "$@"; do
  case $sc in
    clean-up) script=$R/clean_up.sh ;;
    main|ext-*) script=$R/$sc/run.sh ;;
    *) echo "unknown scenario $sc"; continue ;;
  esac
  [ -f "$script" ] || { echo "no $script"; continue; }
  KEEP=1 bash "$script" 2>&1 | mask > "$R/output/$sc.txt"
  echo "$script: exit ${PIPESTATUS[0]}, $(wc -l < "$R/output/$sc.txt") lines in $R/output/$sc.txt"
done
