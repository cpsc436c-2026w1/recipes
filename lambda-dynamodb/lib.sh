# Shared helpers for the lambda-dynamodb recipe. main/run.sh, each ext-*/run.sh and clean_up.sh source this file.
# Builds on the dynamodb recipe: run ../dynamodb/main/run.sh and ../dynamodb/ext-riders-and-drivers/run.sh with
# KEEP=1 first, with the same PREFIX. README.md explains each choice; the section is named in the comment beside it.
# Settings (environment variables):
#   AWS_REGION   default ca-central-1
#   PREFIX       name prefix, default "recipe"; the same one you used for the dynamodb recipe
#   KEEP=1       do not remove the function, URL and role at the end
#   ROLE_ARN     optional: an existing role for the function; the recipe then neither creates nor deletes a role
#   DRY=1        print the commands without running them
#   LOG_DAYS     days the function's log lines are kept, default 7
set -euo pipefail
export AWS_REGION=${AWS_REGION:-ca-central-1} AWS_PAGER=""
# The AWS CLI reads AWS_REGION; boto3, which the handler of step 1 uses on this machine, reads AWS_DEFAULT_REGION.
# Both get the same value, so the handler and the scripts talk to one Region also when your profile names none.
export AWS_DEFAULT_REGION=$AWS_REGION
PREFIX=${PREFIX:-recipe}
FN=$PREFIX-api                 # the Lambda function (README, Decisions: names)
ROLE=$PREFIX-api-role          # the role the function runs as (README, Decisions: role)
LOG_DAYS=${LOG_DAYS:-7}        # how long the function's log lines are kept (README, Decisions: log retention)
ROLE_ARN=${ROLE_ARN:-}         # set it to use a role you already have instead of $ROLE
role_arn() { [ -n "$ROLE_ARN" ] && echo "$ROLE_ARN" || echo "arn:aws:iam::$1:role/$ROLE"; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HANDLER=$HERE/main/handler.py

run() { echo; echo "\$ $*"; [ -n "${DRY:-}" ] || "$@"; }
say() { echo; echo "== $*"; }
account() { [ -n "${DRY:-}" ] && echo "<account>" || aws sts get-caller-identity --query Account --output text; }
# stop with a clear message if the dynamodb recipe's tables are not there. Only DescribeTable's own "not found"
# (ResourceNotFoundException) means that; any other refusal is printed as it is, so that a missing permission or an
# expired sign-in is not read as "run the dynamodb recipe first".
need_table() {
  [ -n "${DRY:-}" ] && return
  local out
  if out=$(aws dynamodb describe-table --table-name "$1" --query Table.TableStatus --output text 2>&1); then return 0; fi
  if grep -q ResourceNotFoundException <<<"$out"; then
    echo "Table $1 not found. Run KEEP=1 bash ../dynamodb/$2/run.sh first." >&2
  else
    echo "${out:-(the command failed without a message)}" >&2
    echo "FAILED: could not look for table $1; the error above is not \"not found\"" >&2
  fi
  exit 1
}

# one create: like run, but when AWS answers that the thing is there already (a second run after KEEP=1), say so and
# go on (IAM: EntityAlreadyExists; CloudWatch Logs: ResourceAlreadyExistsException). Lambda answers
# ResourceConflictException both when the thing already exists and when another change to the
# function is in progress: only the first is "already there"; the second is tried again, at most CREATE_TRIES times
# (default 6, CREATE_WAIT=5 s apart). Any other refusal stops the script.
create() {
  local out try=1 tries=${CREATE_TRIES:-6}
  echo; echo "\$ $*"
  [ -n "${DRY:-}" ] && return 0
  while true; do
    if out=$("$@" 2>&1); then
      [ -z "$out" ] || echo "$out"
      return 0
    elif grep -q -e EntityAlreadyExists -e ResourceAlreadyExistsException <<<"$out" \
        || { grep -q ResourceConflictException <<<"$out" \
             && grep -q -i -e "already exists" -e "FunctionUrlConfig exists" <<<"$out"; }; then
      echo "(already there from an earlier run: kept)"
      return 0
    elif grep -q ResourceConflictException <<<"$out" && grep -q -i "in progress" <<<"$out" && [ "$try" -lt "$tries" ]; then
      echo "(another change to the function is in progress: trying again, attempt $((try + 1)) of $tries)"
      try=$((try + 1))
      sleep "${CREATE_WAIT:-5}"
    else
      echo "$out" >&2
      return 1
    fi
  done
}

# the function's log group, made before the function so that it has a retention period from its first line. Lambda
# would otherwise create the group itself on the first call, with log lines that never expire.
log_group() {
  create aws logs create-log-group --log-group-name "/aws/lambda/$FN" --tags purpose=recipe
  run aws logs put-retention-policy --log-group-name "/aws/lambda/$FN" --retention-in-days "$LOG_DAYS"
}

# one delete: $1 what it removes (for the messages), then the command. "Not found" means already gone; any other
# refusal is printed as FAILED and remembered in FAILED, and the next delete is still attempted.
FAILED=0
remove() {
  local what=$1 out; shift
  echo; echo "\$ $*"
  [ -n "${DRY:-}" ] && return 0
  if out=$("$@" 2>&1); then
    [ -z "$out" ] || echo "$out"
  elif grep -q -e ResourceNotFoundException -e NoSuchEntity <<<"$out"; then
    echo "(no $what: already gone)"
  else
    echo "$out" >&2
    echo "FAILED: the $what was not deleted" >&2
    FAILED=1
  fi
}

# one final check: $1 what it looks for, then a command that prints the exact names of this recipe that are still
# there (empty when none is). Anything printed, or a check that AWS refused, is a FAILED line.
left() {
  local what=$1 out; shift
  echo; echo "\$ $*"
  [ -n "${DRY:-}" ] && return 0
  if ! out=$("$@" 2>&1); then
    echo "$out" >&2
    echo "FAILED: could not check for the $what" >&2
    FAILED=1
  elif [ -n "$(tr -d '[:space:]' <<<"$out")" ]; then
    echo "$out"
    echo "FAILED: still there: $what $out" >&2
    FAILED=1
  else
    echo "(no $what left)"
  fi
}

# remove what this recipe created; the DynamoDB tables stay (../dynamodb/clean_up.sh removes them)
# The URL goes first, so nobody can call the function while the rest is removed (README, Cost and clean-up).
# Every delete is attempted. Returns 1 when AWS refused one, so the script that calls it ends with an error.
clean_api() {
  remove "URL" aws lambda delete-function-url-config --function-name "$FN"
  remove "function" aws lambda delete-function --function-name "$FN"
  remove "log group" aws logs delete-log-group --log-group-name "/aws/lambda/$FN"
  if [ -n "$ROLE_ARN" ]; then
    echo "(ROLE_ARN is set: your own role stays)"
  else
    remove "policy" aws iam delete-role-policy --role-name "$ROLE" --policy-name tables-and-logs
    remove "role" aws iam delete-role --role-name "$ROLE"
  fi
  if [ "$FAILED" != 0 ]; then
    echo >&2
    echo "The clean-up is NOT complete: see the FAILED lines above, fix what they say, and run clean_up.sh again." >&2
    return 1
  fi
}
