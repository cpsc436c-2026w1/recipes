#!/bin/bash
# The live run of s3-event-etl in your lease (ca-central-1), captured with the account masked:
#   PREFIX=etl-yourname bash scripts/live_run.sh [email]                 # the Glue path: writes .local/main.txt (or $OUT)
#   ENGINE=athena PREFIX=etl-yourname bash scripts/live_run.sh [email]   # the Athena path (.local/main-athena.txt)
# up; SIM01 with SIM02's three broken lines (sim/land_cases.py); the checks and the first run; SIM03's copy and an
# event sent twice; with Glue, two runs failed after their MERGE and one hung to a 1-minute timeout, the hold,
# etl.py resume and the run that loads; the counts, the runs' cost, the clean-up and what is left.
# The Glue path costs about 0.12 USD (five runs), the Athena path under 0.01 (README, Cost and clean-up).
set -uo pipefail
cd "$(dirname "$0")/.."
: "${PREFIX:?set PREFIX to a short lowercase prefix no other resource in your lease uses}"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 1
OUT=${OUT:-.local/main$([ "${ENGINE:-}" = athena ] && echo -athena).txt}   # output/ holds the recorded run: a new run goes to .local/ unless OUT says so
mkdir -p "$(dirname "$OUT")"
B="$PREFIX-lake-$ACCT"; RUN=live$(date -u +%H%M%S); DB=${PREFIX//-/_}_lake; ENGINE=${ENGINE:-glue}; export ENGINE
mask() { sed -E -e "s/$ACCT/111122223333/g" -e 's#assumed-role/[^[:space:]/]+(/[^[:space:]]+)?#assumed-role/<role>#g' \
             -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}/you@example.com/g' -e "s#$PWD/##g"; }
sweep_fn() {   # the sweep function under its own role; its log tail
  echo "\$ aws lambda invoke $PREFIX-etl-sweep"
  aws lambda invoke --function-name "$PREFIX-etl-sweep" --log-type Tail --query LogResult --output text /dev/null \
    | base64 -d | grep -v '^START\|^END\|^checked'
}
last_run() { aws glue get-job-runs --job-name "$PREFIX-etl" --max-items 1 --query 'JobRuns[0].Id' --output text | head -1; }
wait_run() {   # until the job's latest run has ended; then its state, time and DPU-seconds
  until s=$(aws glue get-job-run --job-name "$PREFIX-etl" --run-id "$1" --query JobRun.JobRunState --output text)
        [[ $s =~ SUCCEEDED|FAILED|TIMEOUT|ERROR|STOPPED ]]; do sleep 15; done
  aws glue get-job-run --job-name "$PREFIX-etl" --run-id "$1" \
      --query 'JobRun.[JobRunState,ExecutionTime,DPUSeconds,Timeout,ErrorMessage]' --output text
}
glue_run() {   # glue_run [ETL_FAULT] [timeout]: start one from the terminal, wait for its end, the sweep follows it
  before=$(last_run)
  for try in 1 2 3 4 5 6; do                  # Glue refuses a start for a few seconds after a run ends: skipped
    echo "\$ ETL_MIN_GAP_MIN=0 MAX_RUNS_PER_DAY=10 ${1:+ETL_FAULT=$1 }${2:+ETL_FAULT_TIMEOUT_MIN=$2 }scripts/etl.py sweep"
    ETL_MIN_GAP_MIN=0 MAX_RUNS_PER_DAY=10 ETL_FAULT=${1:-} ETL_FAULT_TIMEOUT_MIN=${2:-} uv run python scripts/etl.py sweep
    [ "$(last_run)" != "$before" ] && break; sleep 20
  done
  wait_run "$(last_run)"; sweep_fn
}
query() {      # one Athena query in <PREFIX>-wg: its state, Data scanned and rows
  Q=$(aws athena start-query-execution --work-group "$PREFIX-wg" --query-string "$1" --query QueryExecutionId --output text)
  until s=$(aws athena get-query-execution --query-execution-id "$Q" --query QueryExecution.Status.State --output text)
        [[ $s =~ SUCCEEDED|FAILED|CANCELLED ]]; do sleep 1; done
  aws athena get-query-execution --query-execution-id "$Q" --query 'QueryExecution.[Status.State,Statistics.DataScannedInBytes]' --output text
  aws athena get-query-results --query-execution-id "$Q" --query 'ResultSet.Rows[].Data[].VarCharValue' --output text
}
mkdir -p output
{
  uv run python sim/generate.py SIM01 >/dev/null
  echo "\$ ENGINE=$ENGINE scripts/etl.py up";   uv run python scripts/etl.py up ${1:+--email "$1"}; echo "exit $?"
  uv run python scripts/lake.py workgroup "$PREFIX-wg"
  echo "\$ sim/land_cases.py --run-id $RUN"; uv run python sim/land_cases.py --run-id "$RUN" --bucket "$B"
  echo "\$ wait for the checks (staged objects of this run, as many as landed: 109 to 111)"
  want=$(aws s3 ls "s3://$B/raw/trip-events/" --recursive | grep -c "$RUN.jsonl")
  for i in $(seq 1 40); do
    n=$(aws s3 ls "s3://$B/stage/trip-events/" --recursive | grep -c "$RUN"); [ "$n" -ge "$want" ] && break; sleep 15
  done; echo "staged $n of $want"
  echo "\$ quarantine"; aws s3 ls "s3://$B/quarantine/trip-events-rows/" --recursive | grep "$RUN"
  if [ "$ENGINE" = glue ]; then
    wait_run "$(last_run)"; sweep_fn          # the first run, started by the first check, followed
  fi
  echo "\$ sim/land_cases.py --again, and the created event of the copied object sent again"
  uv run python sim/land_cases.py --run-id "$RUN" --bucket "$B" --again
  K=$(aws s3 ls "s3://$B/raw/trip-events/" --recursive | awk '{print $4}' | grep "$RUN.jsonl" | sort | sed -n 31p)
  SEQ=$(aws dynamodb get-item --table-name "$PREFIX-runs" --key "{\"job\":{\"S\":\"etl-object\"},\"run_id\":{\"S\":\"$K\"}}" \
        --query Item.sequencer.S --output text)
  printf '{"Records":[{"s3":{"bucket":{"name":"%s"},"object":{"key":"%s","sequencer":"%s"}}}]}' "$B" "$K" "$SEQ" > /tmp/$RUN-event.json
  aws lambda invoke --function-name "$PREFIX-etl-check" --payload "fileb:///tmp/$RUN-event.json" --log-type Tail \
      --query LogResult --output text /dev/null | base64 -d | grep '^repeat'
  # grep -c, not grep -q: with pipefail, grep -q ending early fails the pipeline and the loop never ends
  until [ "$(aws s3 ls "s3://$B/stage/trip-events/" --recursive | grep -c -- "$RUN-again-")" -gt 0 ]; do sleep 5; done
  if [ "$ENGINE" = glue ]; then               # the copy waits (the gap): the failed runs below load it, keep it waiting
    glue_run after-merge; glue_run hang 1; glue_run after-merge
    echo "\$ scripts/etl.py sweep   # 3 failed runs in a row"
    ETL_MIN_GAP_MIN=0 MAX_RUNS_PER_DAY=10 uv run python scripts/etl.py sweep
    echo "\$ scripts/etl.py resume"; uv run python scripts/etl.py resume
    glue_run
    echo "\$ scripts/etl.py runs"; uv run python scripts/etl.py runs
  else
    for i in 1 2 3 4 5 6 7 8 9 10; do         # the sweep's MERGE; the hourly sweep may have started it already
      sweep_fn
      s=$(aws dynamodb query --table-name "$PREFIX-runs" --key-condition-expression "job = :j" --no-scan-index-forward \
          --expression-attribute-values '{":j":{"S":"etl"}}' --max-items 1 --query 'Items[0].run_state.S' --output text | head -1)
      [ "$s" = finished ] && break; sleep 30
    done
  fi
  echo "\$ the run log (etl)"
  aws dynamodb query --table-name "$PREFIX-runs" --key-condition-expression "job = :j" \
      --expression-attribute-values '{":j":{"S":"etl"}}' --query 'Items[].[run_id.S,run_state.S,failed_step.S]' --output text
  echo "\$ count: rows, event_ids, from the copy, source_key as a path"
  query "SELECT count(*), count(DISTINCT event_id), count_if(source_key LIKE '%-again-%'), count_if(source_key LIKE 's3://%') FROM $DB.trip_events_clean WHERE event_id LIKE '$RUN%'"
  echo "\$ clean_up.py";         uv run python clean_up.py | tail -3
  echo "\$ clean_up.py --yes";   uv run python clean_up.py --yes; echo "exit $?"
  echo "\$ what is left"
  aws lambda list-functions --query "Functions[?starts_with(FunctionName, '$PREFIX-etl')].FunctionName"
  aws glue list-jobs --query "JobNames[?starts_with(@, '$PREFIX-etl')]"; aws s3 ls "s3://$B/stage/"
  aws scheduler list-schedule-groups --name-prefix "$PREFIX-s3-event-etl" --query 'ScheduleGroups[].[Name,State]'
  aws s3api get-bucket-notification-configuration --bucket "$B"
} 2>&1 | mask | tee "$OUT"
