#!/bin/bash
# dynamodb, extension ext-nightly-report: a nightly report over the trip store from main/, without reading the live
# table. The same report two ways: a Scan through the table (billed per item read), then an export to S3 (no table
# read units) queried in Athena. README.md in this folder says what changes against main/.
#   KEEP=1 bash main/run.sh                                         (first, once)
#   BUCKET=<your bucket> bash ext-nightly-report/run.sh             (the export takes several minutes;
#   cleans up the whole recipe at the end, KEEP=1 to keep the tables)
source "$(dirname "$0")/../lib.sh"
BUCKET=${BUCKET:-${DRY:+my-bucket}}
: "${BUCKET:?set BUCKET to an S3 bucket you own, in ca-central-1}"
need_table "$TRIPS" main
P=$PREFIX/exports
DB=${PREFIX//-/_}_lake

athena() {  # $1 SQL; prints the result rows
  local id s
  echo; echo "\$ athena: $1"
  [ -n "${DRY:-}" ] && return
  id=$(aws athena start-query-execution --query-string "$1" \
    --result-configuration "OutputLocation=s3://$BUCKET/$PREFIX/athena-results/" --query QueryExecutionId --output text)
  while s=$(aws athena get-query-execution --query-execution-id "$id" --query QueryExecution.Status.State --output text); \
        [ "$s" = QUEUED ] || [ "$s" = RUNNING ]; do sleep 2; done
  aws athena get-query-execution --query-execution-id "$id" \
    --query 'QueryExecution.{state: Status.State, reason: Status.StateChangeReason, scanned_bytes: Statistics.DataScannedInBytes}'
  if [ "$s" != SUCCEEDED ]; then
    # A query that did not succeed ends the run here, with what stays said, instead of a later step reading nothing.
    echo "FAILED: the Athena query ended in state ${s:-(none)}. Nothing was cleaned up: fix the cause and run again, or run: BUCKET=$BUCKET bash clean_up.sh" >&2
    exit 1
  fi
  aws athena get-query-results --query-execution-id "$id" --query 'ResultSet.Rows[].Data[*].VarCharValue' --output text
}

say "1. The report the slow way: Scan reads every trip through the table, and every item read is billed"
run aws dynamodb scan --table-name "$TRIPS" --projection-expression 'pickup_zone, fare_quote' \
  --return-consumed-capacity TOTAL --query '{items_read: ScannedCount, read_units: ConsumedCapacity.CapacityUnits}'

say "2. Exports need point-in-time recovery (PITR) switched on for the table"
run aws dynamodb update-continuous-backups --table-name "$TRIPS" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
  --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus'

# Decision (reports): the export reads the PITR backup, not the table, so the app's reads are not slowed.
say "3. Start a full export to S3. It does not use the table's read units"
if [ -n "${DRY:-}" ]; then ARN="arn:aws:dynamodb:$AWS_REGION:<account>:table/$TRIPS"; else ARN=$(aws dynamodb describe-table --table-name "$TRIPS" --query Table.TableArn --output text); fi
run aws dynamodb export-table-to-point-in-time --table-arn "$ARN" --s3-bucket "$BUCKET" --s3-prefix "$P" \
  --export-format DYNAMODB_JSON --query 'ExportDescription.{status: ExportStatus, arn: ExportArn}'
# Under DRY=1 nothing was started, so the export's ARN and its folder are placeholders in the printed commands.
if [ -n "${DRY:-}" ]; then EXP="<the export's ARN>"; else
EXP=$(aws dynamodb list-exports --table-arn "$ARN" --query 'sort_by(ExportSummaries, &ExportArn)[-1].ExportArn' --output text)
fi

say "4. Wait for it (checks every 30 seconds)"
if [ -z "${DRY:-}" ]; then
start=$(date +%s)
while s=$(aws dynamodb describe-export --export-arn "$EXP" --query ExportDescription.ExportStatus --output text); [ "$s" = IN_PROGRESS ]; do sleep 30; done
echo "status $s after $(( $(date +%s) - start )) s of waiting"
if [ "$s" != COMPLETED ]; then
  # An export that did not complete has no data files: the later steps would build a table over nothing.
  echo "FAILED: the export ended with status ${s:-(none)}, not COMPLETED. Nothing was cleaned up: look at the export in the console, then run again, or run: BUCKET=$BUCKET bash clean_up.sh" >&2
  exit 1
fi
fi
run aws dynamodb describe-export --export-arn "$EXP" \
  --query 'ExportDescription.{status: ExportStatus, items: ItemCount, billed_bytes: BilledSizeBytes, start: StartTime, end: EndTime, manifest: ExportManifest}'

say "5. What landed in S3: a manifest and gzipped JSON data files"
if [ -n "${DRY:-}" ]; then D="$P/AWSDynamoDB/<export id>"; else
D=$(aws dynamodb describe-export --export-arn "$EXP" --query ExportDescription.ExportManifest --output text | sed 's#/manifest-summary.json##')
fi
run aws s3 ls --recursive "s3://$BUCKET/$D/"

say "6. A table in Athena over the data files. Each line is {\"Item\": {attribute: {type: value}}}"
athena "CREATE DATABASE IF NOT EXISTS $DB"
athena "DROP TABLE IF EXISTS $DB.trips_export"
athena "CREATE EXTERNAL TABLE $DB.trips_export (
  Item struct<trip_id:struct<S:string>, status:struct<S:string>, pickup_zone:struct<S:string>,
              fare_quote:struct<N:string>, driver_id:struct<S:string>>)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://$BUCKET/$D/data/'"

say "7. Trips per pickup zone, from the copy (the live table is not touched)"
athena "SELECT Item.pickup_zone.S AS zone, count(*) AS trips, avg(CAST(Item.fare_quote.N AS double)) AS avg_fare
FROM $DB.trips_export GROUP BY 1 ORDER BY 1"

finish
