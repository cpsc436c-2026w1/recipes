#!/bin/bash
# lambda-dynamodb, extension ext-read-path: the p50, p95 and p99 of each read of the API, warm, cold, after a quiet
# time and on a fresh copy of the function. README.md in this folder says what it changes (only the busy rider's
# items and one environment variable, both undone by the same run).
#   KEEP=1 bash main/run.sh                       (first, once: the function and its URL)
#   bash ext-read-path/run.sh                     600 planned calls, one at a time
#   bash ext-read-path/run.sh --busy-rider --writer 5
#   bash ext-read-path/run.sh --idle              one after-idle call per start, when its gap has passed
#   bash ext-read-path/run.sh --no-plan --fresh 20   after the idle round: a fresh copy is a configuration change
#   bash ext-read-path/run.sh --summary           the summary of results.duckdb
#   DRY=1 bash ext-read-path/run.sh ...           prints what it would do, calls nothing
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export AWS_REGION=${AWS_REGION:-ca-central-1} AWS_PAGER=""
export AWS_DEFAULT_REGION=$AWS_REGION PREFIX=${PREFIX:-recipe}
PY=${PY:-uv run --project "$HERE/.." python}       # PY=python3 where boto3 and duckdb are installed already
if [ -n "${DRY:-}" ]; then
  echo "\$ aws lambda get-function-url-config --function-name $PREFIX-api"
  echo "(DRY=1) then: the planned calls of $HERE/variants.toml, one at a time with curl, to <url>; $*"
  echo "\$ aws logs filter-log-events --log-group-name /aws/lambda/$PREFIX-api --filter-pattern \"REPORT RequestId\""
  exit 0
fi
exec $PY "$HERE/read_path.py" "$@"
