# Shared helpers for the dynamodb recipe. main/run.sh, each ext-*/run.sh and clean_up.sh source this file.
# README.md explains each choice; the section is named in the comment beside it.
# Settings (environment variables):
#   AWS_REGION   default ca-central-1
#   PREFIX       name prefix, default "recipe"; give each teammate their own if you share an account
#   KEEP=1       do not clean up at the end, so an extension can build on what main/ made
#   BUCKET       an S3 bucket you own, in ca-central-1 (ext-nightly-report only)
#   DRY=1        print the commands without running them
#   MAX_UNITS    the most read and write request units per second each table takes, default 100
#   WAIT_MINUTES how long ext-vector-search waits for its vector index to become ACTIVE, default 30
#   KB_MCP_TOKEN, COURSE_GATEWAY_URL  ext-vector-search only: set both to make the vectors through the course LLM gateway
set -euo pipefail
export AWS_REGION=${AWS_REGION:-ca-central-1} AWS_PAGER=""
PREFIX=${PREFIX:-recipe}
# Every name starts with $PREFIX, and every table carries the tags purpose=recipe and prefix=$PREFIX (README,
# Decisions: names). One table per access pattern (README, Decisions: keys).
# A ceiling on each table's on-demand throughput, in request units per second (README, Decisions: capacity). A loop
# that writes without end is throttled at this rate instead of billing without limit.
MAX_UNITS=${MAX_UNITS:-100}
TRIPS=$PREFIX-trips              # partition key trip_id                         (main)
RIDER_TRIPS=$PREFIX-rider-trips  # partition key rider_id, sort key requested_at (ext-riders-and-drivers)
DRIVERS=$PREFIX-drivers          # partition key driver_id                       (ext-riders-and-drivers)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

run() { echo; echo "\$ $*"; [ -n "${DRY:-}" ] || "$@"; }
say() { echo; echo "== $*"; }

# create an on-demand table: $1 name, $2 key schema, $3 attribute definitions, $4 (optional) global secondary indexes
# On-demand (PAY_PER_REQUEST): billed per request, nothing to size, no charge for throughput while idle; stored
# items are still billed (README, Decisions: capacity).
# A table that is already there is kept (a second run, or an extension after KEEP=1): create-table refuses a table
# that exists with ResourceInUseException (API reference, CreateTable). One that an earlier clean-up is still
# deleting is waited out first, so that the new table is not created under a name that is still on its way out.
# Only DescribeTable's own "not found" (ResourceNotFoundException) means the table is not there. Any other refusal
# (no permission, an expired sign-in) stops the run: creating a table after a look that failed would hide the cause.
table() {
  if [ -z "${DRY:-}" ]; then
    local status out
    if out=$(aws dynamodb describe-table --table-name "$1" --query Table.TableStatus --output text 2>&1); then
      status=$out
    elif grep -q ResourceNotFoundException <<<"$out"; then
      status=""
    else
      echo "${out:-(the command failed without a message)}" >&2
      echo "FAILED: could not look for table $1; the error above is not \"not found\"" >&2
      exit 1
    fi
    if [ "$status" = DELETING ]; then
      echo; echo "(table $1 is still being deleted by an earlier clean-up: waiting until it is gone)"
      aws dynamodb wait table-not-exists --table-name "$1"
    elif [ -n "$status" ]; then
      echo; echo "(table $1 is already there: kept as it is)"
      aws dynamodb wait table-exists --table-name "$1"
      return 0
    fi
  fi
  local index=()
  [ -z "${4:-}" ] || index=(--global-secondary-indexes "$4")
  run aws dynamodb create-table --table-name "$1" --billing-mode PAY_PER_REQUEST \
    --on-demand-throughput "MaxReadRequestUnits=$MAX_UNITS,MaxWriteRequestUnits=$MAX_UNITS" \
    --key-schema "$2" --attribute-definitions "$3" "${index[@]}" --tags Key=purpose,Value=recipe "Key=prefix,Value=$PREFIX" --query TableDescription.TableStatus
  [ -n "${DRY:-}" ] || aws dynamodb wait table-exists --table-name "$1"
}

# one batch write: $1 the request items (JSON, at most 25). DynamoDB may write only part of a batch and return the
# rest as UnprocessedItems; those are sent again, after 1, 2 and 4 s, in at most BATCH_CALLS calls in all (default 4).
# If items are still unprocessed then, the script stops: a load that is short would make every later number wrong.
batch_write() {
  local items=$1 out call=1 calls=${BATCH_CALLS:-4} rest=${TMPDIR:-/tmp}/$PREFIX-unprocessed.json
  while true; do
    echo; echo "\$ aws dynamodb batch-write-item --request-items $items --return-consumed-capacity TOTAL --output json"
    [ -n "${DRY:-}" ] && return 0
    out=$(aws dynamodb batch-write-item --request-items "$items" --return-consumed-capacity TOTAL --output json)
    python3 -c "
import json, sys
reply = json.loads(sys.argv[1])
left = {t: r for t, r in (reply.get('UnprocessedItems') or {}).items() if r}
units = sum(c.get('CapacityUnits', 0) for c in reply.get('ConsumedCapacity') or [])
print(json.dumps({'unprocessed': sum(len(r) for r in left.values()), 'write_units': units}, indent=4))
json.dump(left, open(sys.argv[2], 'w'))" "$out" "$rest"
    items=$(cat "$rest")
    [ "$items" != "{}" ] || return 0
    if [ "$call" -ge "$calls" ]; then
      echo "FAILED: DynamoDB still returned unprocessed items after $calls calls; the load is not complete" >&2
      return 1
    fi
    echo "(DynamoDB returned unprocessed items: sending them again in $((1 << (call - 1))) s, call $((call + 1)) of $calls)"
    sleep "$((1 << (call - 1)))"
    call=$((call + 1))
  done
}

# stop with a clear message if main/ has not been run with KEEP=1 first. Only DescribeTable's own "not found"
# (ResourceNotFoundException) means that; any other refusal is printed as it is, so that a missing permission or
# an expired sign-in is not read as "run main first".
need_table() {
  [ -n "${DRY:-}" ] && return
  local out
  if out=$(aws dynamodb describe-table --table-name "$1" --query Table.TableStatus --output text 2>&1); then return 0; fi
  if grep -q ResourceNotFoundException <<<"$out"; then
    echo "Table $1 not found. Run $2 first with KEEP=1, for example: KEEP=1 bash $2/run.sh" >&2
  else
    echo "${out:-(the command failed without a message)}" >&2
    echo "FAILED: could not look for table $1; the error above is not \"not found\"" >&2
  fi
  exit 1
}

# The scripts ask the AWS CLI for errors as JSON (--cli-error-format json), so that a refused write shows the item
# that blocked it. An AWS CLI that does not know the option would stop at the first accept with "Unknown options",
# with the table already made; say so before anything is created. `aws configure list` reads local settings only:
# it makes no call to AWS. We ran AWS CLI 2.39.3.
need_cli() {
  [ -n "${DRY:-}" ] && return
  aws configure list --cli-error-format json >/dev/null 2>&1 && return
  echo "FAILED: this AWS CLI ($(aws --version 2>&1 | head -1)) does not know --cli-error-format. Update the AWS CLI (we ran 2.39.3), then run again" >&2
  exit 1
}

# a write that its condition may refuse: $1 what to print when it does; the rest is the command.
# Only the condition's own error is "refused": ConditionalCheckFailedException, or ConditionalCheckFailed as the
# reason of a cancelled transaction. Any other error (no permission, a throttle, an expired sign-in) stops the run
# with FAILED, so that it is not read as a refusal.
or_refused() {
  local words=$1 err
  shift
  err=$(mktemp)
  if "$@" 2>"$err"; then rm -f "$err"; return 0; fi
  cat "$err" >&2
  if grep -q ConditionalCheckFailed "$err"; then rm -f "$err"; echo "($words)"; return 0; fi
  rm -f "$err"
  echo "FAILED: the write failed, and not because its condition refused it; the error above says why" >&2
  exit 1
}

# last step of every scenario: clean up, unless KEEP=1 (README, Cost and clean-up)
finish() {
  if [ -n "${KEEP:-}" ]; then
    say "Done. KEEP=1: everything stays for the extensions. Clean up later with: bash clean_up.sh"
  else
    say "Clean up (run with KEEP=1 to keep everything for the extensions)"
    bash "$HERE/clean_up.sh"
  fi
}
