#!/bin/bash
# lambda-dynamodb: remove the API (URL, function, logs, role) after a KEEP=1 run. The tables stay:
# ../dynamodb/clean_up.sh removes them.
#   bash clean_up.sh
# Every delete is attempted; if AWS refused one, the script prints FAILED lines and ends with exit code 1.
source "$(dirname "$0")/lib.sh"
say "Remove the URL, the function, its log group and its role"
clean_api || FAILED=1
say "Check: no function, log group or role of this recipe left (each check prints \"(no ... left)\")"
left "log group" aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$FN" --output text \
  --query "logGroups[?logGroupName=='/aws/lambda/$FN'].logGroupName"
[ -n "$ROLE_ARN" ] || left "role" aws iam list-roles --output text --query "Roles[?RoleName=='$ROLE'].RoleName"
left "function" aws lambda list-functions --output text --query "Functions[?FunctionName=='$FN'].FunctionName"
[ "$FAILED" = 0 ]
