#!/bin/bash
# The recipe's own live demonstration of ext-backfill (ca-central-1), captured with the account masked. It ends with
# clean_up.py --yes, so nothing it made is left for a project to use; for a project run, use the step-by-step
# commands of the README.
#   PREFIX=backfill-yourname bash ext-backfill/live_run.sh     # writes .local/ext-backfill-live.txt (OUT= to change)
# The CTAS of DS01's 30 days from the course catalog and its count; a wider window (one INSERT INTO a month); a short
# day made by deleting one of its files, found by --check-only and copied again; the import into <PREFIX>-trips-by-id;
# BatchWriteItem of one day into that table; then the clean-up and what is left. About 0.07 USD (README, Cost).
set -uo pipefail
cd "$(dirname "$0")/.."
: "${PREFIX:?set PREFIX to a short lowercase prefix no other resource in your lease uses}"
OUT=${OUT:-.local/ext-backfill-live.txt}   # output/ext-backfill.txt is the recorded run: not overwritten unless OUT says so
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
B="$PREFIX-lake-$ACCT"
mask() { sed -E -e "s/$ACCT/111122223333/g" \
             -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' -e "s#$PWD/##g"; }
bf() { echo "\$ backfill.py $*"; uv run python ext-backfill/backfill.py --target aws "$@"; echo "exit $?"; }
mkdir -p "$(dirname "$OUT")"
{
  bf --source course --from 2024-03-01 --to 2024-03-30
  bf --source course --from 2024-03-01 --to 2024-04-02            # 30 complete; INSERT INTO for March 31 and April
  F=$(aws s3 ls "s3://$B/history/trips/requested_day=2024-03-14/" | awk '{print $4}' | sort | tail -1)
  echo "\$ aws s3 rm one of the files of requested_day=2024-03-14   # a short day"
  aws s3 rm "s3://$B/history/trips/requested_day=2024-03-14/$F" >/dev/null
  bf --check-only --from 2024-03-01 --to 2024-04-02               # exit 1: the day differs
  bf --source course --from 2024-03-01 --to 2024-04-02            # the short day deleted and copied again
  bf --to-dynamodb                                                # a new table: import from S3
  bf --to-dynamodb --from 2024-04-02 --to 2024-04-02 --load-id batch-0402   # a table that exists: BatchWriteItem
  echo "\$ aws dynamodb get-item (one trip by its id)"
  ID=$(aws dynamodb scan --table-name "$PREFIX-trips-by-id" --max-items 1 --query 'Items[0].trip_id.S' --output text | head -1)
  aws dynamodb get-item --table-name "$PREFIX-trips-by-id" --key "{\"trip_id\": {\"S\": \"$ID\"}}" --output json
  echo "\$ clean_up.py"
  uv run python ext-backfill/clean_up.py | tail -3
  echo "\$ clean_up.py --yes"
  uv run python ext-backfill/clean_up.py --yes; echo "exit $?"
  echo "\$ what is left: tables, Glue tables, prefixes"
  aws dynamodb describe-table --table-name "$PREFIX-trips-by-id" --query Table.TableStatus --output text 2>&1 | tail -1
  aws glue get-tables --database-name "${PREFIX//-/_}_lake" --query 'TableList[].Name' 2>&1 | head -5
  aws s3 ls "s3://$B/history/"; aws s3 ls "s3://$B/imports/"
} 2>&1 | mask | tee "$OUT"
