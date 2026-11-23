#!/usr/bin/env bash
# The live run of ext-sdk-lambda, from the recipe folder (agent-agentcore/), in your team sandbox, ca-central-1.
#
#   bash ext-sdk-lambda/run.sh            build the data and the package, deploy, ask one question, show the 403
#   DRY=1 bash ext-sdk-lambda/run.sh      print the commands; run nothing
#
# Needs ext-sdk-lambda/.env (copy .env.example; set COURSE_GATEWAY_URL and KB_MCP_TOKEN) and AWS credentials.
# Clean up after: uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py --yes
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=ext-sdk-lambda/.env
MONTH=build/fhvhv_tripdata_2024-03.parquet
MONTH_URL=https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet

run() { echo "+ $*"; if [ "${DRY:-0}" != "1" ]; then "$@"; fi; }

[ -f "$ENV" ] || { echo "copy ext-sdk-lambda/.env.example to $ENV and fill it in first"; exit 1; }
mkdir -p build
[ -f "$MONTH" ] || run curl -fsSL -o "$MONTH" "$MONTH_URL"
run uv run python ext-sdk-lambda/make_trips.py --from-tlc "$MONTH" --out build/trips.parquet
run uv run --env-file "$ENV" python ext-sdk-lambda/deploy.py package
run uv run --env-file "$ENV" python ext-sdk-lambda/deploy.py up
run uv run --env-file "$ENV" python ext-sdk-lambda/invoke_url.py "How many trips were requested on 2024-03-14?"
run uv run --env-file "$ENV" python ext-sdk-lambda/invoke_url.py --unsigned "How many trips?" || true
