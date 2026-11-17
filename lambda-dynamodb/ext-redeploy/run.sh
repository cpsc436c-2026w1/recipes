#!/bin/bash
# lambda-dynamodb, extension ext-redeploy: deploy a changed main/handler.py to the function main/ made, and call it once.
# README.md in this folder says what changes against main/.
#   KEEP=1 bash main/run.sh            (first, once)
#   bash ext-redeploy/run.sh           (after each change to main/handler.py)
# Decision (redeploy): replace the code of the existing function, so its URL, role and logs stay the same.
source "$(dirname "$0")/../lib.sh"
A=$(account)
ZIP=${TMPDIR:-/tmp}/$FN.zip

say "1. Package: a zip with handler.py at its top level (boto3 is already in the Lambda Python runtime)"
run python3 -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'w'); z.write(sys.argv[2],'handler.py'); z.close()" "$ZIP" "$HANDLER"

if [ -z "${DRY:-}" ] && aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  say "2. The function exists: replace its code"
  run aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$ZIP" --query '{state: State, update: LastUpdateStatus}'
  run aws lambda wait function-updated-v2 --function-name "$FN"
else
  say "2. Create the function: Python 3.12 on arm64 (Graviton), 128 MB, 10 s timeout, the role from main/run.sh (or ROLE_ARN)"
  log_group
  sleep "${ROLE_WAIT:-10}"   # a role created seconds ago may not be usable by Lambda yet
  run aws lambda create-function --function-name "$FN" --runtime python3.12 --architectures arm64 \
    --memory-size 128 --timeout 10 --handler handler.lambda_handler \
    --role "$(role_arn "$A")" --zip-file "fileb://$ZIP" \
    --environment "Variables={PREFIX=$PREFIX}" --tags purpose=recipe \
    --query '{name: FunctionName, runtime: Runtime, arch: Architectures[0], memory_mb: MemorySize, state: State}'
  run aws lambda wait function-active-v2 --function-name "$FN"
fi

say "3. Call it directly once (an event, the way another AWS service would call it)"
run aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
  --payload '{"action":"get","trip_id":"t-0042","strong":true}' "${TMPDIR:-/tmp}/$FN-out.json" \
  --query '{status: StatusCode, error: FunctionError}'
[ -n "${DRY:-}" ] || cat "${TMPDIR:-/tmp}/$FN-out.json"; echo
