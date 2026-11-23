#!/bin/bash
# lambda-dynamodb, extension ext-dashboard-page: a page of requests by zone, with each zone's freshness, served by one
# Lambda function from DynamoDB. README.md in this folder says what it adds to main/ (nothing in main/ changes).
#   bash ext-dashboard-page/run.sh                 the counts table, the page function and its role (no public URL)
#   bash ext-dashboard-page/run.sh --lake          also the hourly job that runs the Athena summary query
#   bash ext-dashboard-page/run.sh load rush-hour  count a simulated hour into the table, ending now
#   bash ext-dashboard-page/run.sh url-on 3        a public URL for 3 hours: its deletion is scheduled first
#   bash ext-dashboard-page/run.sh url-off         the URL and its schedule, now
#   DRY=1 bash ext-dashboard-page/run.sh ...       prints every call, makes none
# Remove everything with: uv run python clean_up.py --yes  (from lambda-dynamodb/)
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export AWS_REGION=${AWS_REGION:-ca-central-1} AWS_PAGER=""
export AWS_DEFAULT_REGION=$AWS_REGION PREFIX=${PREFIX:-recipe}
PY=${PY:-uv run --project "$HERE/.." python}       # PY=python3 where boto3 and pydantic are installed already
case "${1:-up}" in
  up|--lake) [ "${1:-}" = "--lake" ] && exec $PY "$HERE/deploy.py" up --lake || exec $PY "$HERE/deploy.py" up ;;
  url-on)    exec $PY "$HERE/deploy.py" url-on "${2:?how many hours, for example: url-on 3}" ;;
  url-off)   exec $PY "$HERE/deploy.py" url-off ;;
  load)      exec $PY "$HERE/load_counts.py" "${2:-rush-hour}" --end-at now ;;
  serve)     exec $PY "$HERE/serve_local.py" ;;
  bench)     exec $PY "$HERE/bench.py" "${2:?the page URL}" ;;
  *) echo "usage: run.sh [--lake] | load <data set> | serve | url-on <hours> | url-off | bench <url>" >&2; exit 2 ;;
esac
