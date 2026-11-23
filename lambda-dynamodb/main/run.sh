#!/bin/bash
# lambda-dynamodb, main scenario: an HTTPS API for the trip store, end to end.
# Test the handler on your machine, give it a role that can do only what it needs, deploy it, put a public URL in front,
# call it the way an app would, read what each call was billed, then remove it all.
# README.md, "Main scenario" and "Decisions", explains each step.
#   KEEP=1 bash ../dynamodb/main/run.sh && KEEP=1 bash ../dynamodb/ext-riders-and-drivers/run.sh
#   bash main/run.sh            (KEEP=1 keeps the API; the tables stay either way)
#   DRY=1 bash main/run.sh      (prints the commands, runs nothing)
source "$(dirname "$0")/../lib.sh"
need_table "$PREFIX-trips" main
need_table "$PREFIX-drivers" ext-riders-and-drivers
A=$(account)
T="arn:aws:dynamodb:$AWS_REGION:$A:table"

say "1. Run the handler on this machine first, against your tables (needs Python 3 and boto3; CloudShell has both)"
run env PREFIX="$PREFIX" python3 "$HANDLER"

if [ -n "$ROLE_ARN" ]; then
  say "2-3. ROLE_ARN is set: the function runs as $ROLE_ARN, which needs the permissions in README.md, Decisions (role)"
else
say "2. A role for the function. The trust policy says who may use it: the Lambda service"
create aws iam create-role --role-name "$ROLE" --tags Key=purpose,Value=recipe \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' --query Role.Arn

# Decision (role): least privilege. Three DynamoDB actions on three named tables and the function's own log group;
# no wildcard actions, no other tables. TransactWriteItems is not an IAM action: each item's UpdateItem authorises it.
say "3. What the role may do: read and update the three tables, and write its own logs. Nothing else"
POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
  {"Sid":"TripsAndDrivers","Effect":"Allow",
   "Action":["dynamodb:GetItem","dynamodb:Query","dynamodb:UpdateItem"],
   "Resource":["$T/$PREFIX-trips","$T/$PREFIX-rider-trips","$T/$PREFIX-drivers"]},
  {"Sid":"OwnLogs","Effect":"Allow",
   "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
   "Resource":"arn:aws:logs:$AWS_REGION:$A:log-group:/aws/lambda/$FN:*"}]}
EOF
)
run aws iam put-role-policy --role-name "$ROLE" --policy-name tables-and-logs --policy-document "$POLICY"
fi

# Decision (function size): arm64 and 128 MB, the smallest memory; Lambda bills per GB-second of configured memory.
say "4. The function's log group, kept $LOG_DAYS days; then package and create the function: Python 3.12 on arm64, 128 MB, 10 s timeout (boto3 is in the runtime already)"
log_group
ZIP=${TMPDIR:-/tmp}/$FN.zip
run python3 -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'w'); z.write(sys.argv[2],'handler.py'); z.close()" "$ZIP" "$HANDLER"
if [ -z "${DRY:-}" ] && aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  # a second run after KEEP=1: the function is there, so its code is replaced (what ext-redeploy does)
  echo; echo "(function $FN is already there from an earlier run: replacing its code)"
  run aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$ZIP" --query '{state: State, update: LastUpdateStatus}'
  run aws lambda wait function-updated-v2 --function-name "$FN"
else
[ -n "${DRY:-}" ] || sleep "${ROLE_WAIT:-10}"   # a role created seconds ago may not be usable by Lambda yet
run aws lambda create-function --function-name "$FN" --runtime python3.12 --architectures arm64 \
  --memory-size 128 --timeout 10 --handler handler.lambda_handler --role "$(role_arn "$A")" \
  --zip-file "fileb://$ZIP" --environment "Variables={PREFIX=$PREFIX}" --tags purpose=recipe \
  --query '{name: FunctionName, runtime: Runtime, arch: Architectures[0], memory_mb: MemorySize, state: State}'
run aws lambda wait function-active-v2 --function-name "$FN"
fi

say "5. Call it directly once, with an event (the way another AWS service would call it)"
run aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
  --payload '{"action":"get","trip_id":"t-0042","strong":true}' "${TMPDIR:-/tmp}/$FN-out.json" --query '{status: StatusCode, error: FunctionError}'
[ -n "${DRY:-}" ] || { cat "${TMPDIR:-/tmp}/$FN-out.json"; echo; }

# Decision (front door): a function URL, not API Gateway: one function, one address, nothing else to pay for.
# Auth type NONE is for the demo only; README, "Adapt it", says how to switch to AWS_IAM.
say "6. A public HTTPS address: a function URL with auth type NONE. Anyone who has the URL can call it"
create aws lambda create-function-url-config --function-name "$FN" --auth-type NONE --query '{url: FunctionUrl, auth: AuthType}'
say "7. A public URL needs two statements in the function's resource policy: invoke through the URL, and invoke at all only via the URL"
create aws lambda add-permission --function-name "$FN" --statement-id url-public --action lambda:InvokeFunctionUrl \
  --principal '*' --function-url-auth-type NONE --query Statement --output text
create aws lambda add-permission --function-name "$FN" --statement-id url-invoke --action lambda:InvokeFunction \
  --principal '*' --invoked-via-function-url --query Statement --output text

if [ -z "${DRY:-}" ]; then
  URL=$(aws lambda get-function-url-config --function-name "$FN" --query FunctionUrl --output text)
  sleep 5   # a new URL and its policy take a few seconds to answer
fi
# Each request is printed with <url> in place of the address (under DRY=1 there is no address yet), then sent.
call() {
  echo; echo "\$ curl -X POST <url> -d '$1'"
  [ -n "${DRY:-}" ] || curl -s -w '   [HTTP %{http_code}, %{time_total} s]\n' -X POST "$URL" -H 'content-type: application/json' -d "$1"
}
say "8. Call it the way the app would"
call '{"action":"get","trip_id":"t-0042"}'
call '{"action":"rider","rider_id":"r-03","since":"2026-10-08"}'
call '{"action":"claim","trip_id":"t-0042","driver_id":"d-9"}'
call '{"action":"accept","trip_id":"t-0101","driver_id":"d-3"}'
call '{"action":"delete-everything"}'

say "9. What each call was billed: the REPORT line Lambda logs after every call (logs can take a few seconds to arrive)"
[ -n "${DRY:-}" ] || sleep 15
run aws logs filter-log-events --log-group-name "/aws/lambda/$FN" --filter-pattern '"REPORT RequestId"' \
  --query 'events[].message' --output text

if [ -n "${KEEP:-}" ]; then
  say "Done. KEEP=1: the API stays up and public. Remove it with: bash clean_up.sh (it lists), then bash clean_up.sh --yes"
else
  say "10. Remove the URL first (so nobody can call it), then the function, its logs and its role. The tables stay"
  clean_api
fi
