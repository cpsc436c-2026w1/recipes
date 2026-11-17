#!/bin/bash
# lambda-dynamodb, extension ext-metrics: where the function's logs are, what each call was billed, and the
# CloudWatch metrics Lambda keeps for every function. README.md in this folder says what changes against main/.
#   KEEP=1 bash main/run.sh            (first, once: the calls in its step 8 are what this reads)
#   bash ext-metrics/run.sh
# Decision (observability): read what AWS already records (REPORT lines, AWS/Lambda metrics) before adding your own.
source "$(dirname "$0")/../lib.sh"
G=/aws/lambda/$FN

say "1. Every function writes to its own log group"
run aws logs describe-log-groups --log-group-name-prefix "$G" --query 'logGroups[].{name: logGroupName, stored_bytes: storedBytes}'

say "2. The REPORT line Lambda writes after every call: duration, billed duration, memory size and memory used"
run aws logs filter-log-events --log-group-name "$G" --filter-pattern '"REPORT RequestId"' --max-items 8 \
  --query 'events[].message' --output text

say "3. Calls in the last hour, and their average and longest duration (CloudWatch metrics)"
S=$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1H +%Y-%m-%dT%H:%M:%SZ); E=$(date -u +%Y-%m-%dT%H:%M:%SZ)
run aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value="$FN" \
  --start-time "$S" --end-time "$E" --period 3600 --statistics Sum --query 'Datapoints[].Sum'
run aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration --dimensions Name=FunctionName,Value="$FN" \
  --start-time "$S" --end-time "$E" --period 3600 --statistics Average Maximum --query 'Datapoints[].{avg_ms: Average, max_ms: Maximum}'
