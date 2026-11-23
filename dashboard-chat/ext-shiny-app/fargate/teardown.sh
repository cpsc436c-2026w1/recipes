#!/bin/bash
# Remove everything deploy.sh made: running tasks, the task definitions of the family $PREFIX-chat-app (no other
# family), the cluster, the security group, the log group, the ECR repository with its images, the secret, and the
# execution role if deploy.sh made it. Every step is tried, also after one fails. A step whose resource is already
# gone is passed over. If a step fails, or a look-up is refused (so that nobody knows whether the thing is there),
# the script says which and exits with code 1: something may be left that bills or blocks a later deploy, so do
# not read the run as done.
# A bare run lists what it would delete (the commands, as DRY=1 prints them) and deletes nothing; --yes deletes.
if [ "${1:-}" = "--yes" ]; then shift; else export DRY=1; fi
source "$(dirname "$0")/common.sh"
guard
FAILED=0
# try: run one removing command; a failure is written down and the script goes on
try() { run "$@" || { FAILED=1; echo "FAILED: $*" >&2; }; }
# look: a look-up whose answer is its output (a list that may be empty); if it is refused, that is a failed step
look() { "$@" || { FAILED=1; echo "FAILED look-up: ${*:2}" >&2; }; }

bash "$HERE/stop.sh" || { FAILED=1; echo "FAILED: stop.sh" >&2; }

# Task definitions: only the family named exactly $NAME. --family-prefix alone would also match a teammate's
# $NAME-2, so the ARNs are filtered on ":task-definition/$NAME:". Deregister first, then delete.
for status in ACTIVE INACTIVE; do
  TDS=$(look ask "arn:aws:ecs:$AWS_REGION:111122223333:task-definition/$NAME:1" aws ecs list-task-definitions --family-prefix "$NAME" --status $status \
        --query "taskDefinitionArns[?contains(@, ':task-definition/$NAME:')]" --output text)
  for td in $TDS; do
    [ $status = INACTIVE ] || try aws ecs deregister-task-definition --task-definition "$td" --query taskDefinition.status --output text
    try aws ecs delete-task-definitions --task-definitions "$td" --query 'taskDefinitions[0].status' --output text
  done
  [ -z "${DRY:-}" ] || break
done

# DescribeClusters does not fail for a cluster that is gone: it returns no cluster. So the name is read from the output.
CLUSTER=$(look ask "$NAME" aws ecs describe-clusters --clusters "$NAME" --query 'clusters[?status==`ACTIVE`].clusterName | [0]' --output text)
if [ "$CLUSTER" = "$NAME" ]; then
  try aws ecs delete-cluster --cluster "$NAME" --query cluster.status --output text
fi

SG=$(look sg_id)
[ -n "${DRY:-}" ] && SG=sg-EXAMPLE
if [ "$SG" != None ] && [ -n "$SG" ]; then
  # The task's network interface is released a little after the task stops; until then the group is in use.
  # Six tries, 20 seconds apart; if the last one fails too, that is a failed step.
  REMOVED=""
  for attempt in 1 2 3 4 5 6; do
    if run aws ec2 delete-security-group --group-id "$SG"; then REMOVED=yes; break; fi
    [ $attempt = 6 ] || sleep 20
  done
  [ -n "$REMOVED" ] || { FAILED=1; echo "FAILED: the security group $SG is still in use after 6 tries" >&2; }
fi

if present aws logs describe-log-streams --log-group-name "$LOG_GROUP" --limit 1; then
  try aws logs delete-log-group --log-group-name "$LOG_GROUP"
fi
if present aws ecr describe-repositories --repository-names "$NAME"; then
  try aws ecr delete-repository --repository-name "$NAME" --force --query repository.repositoryName --output text
fi
if present aws secretsmanager describe-secret --secret-id "$SECRET_NAME"; then
  try aws secretsmanager delete-secret --secret-id "$SECRET_NAME" --force-delete-without-recovery --query Name --output text
fi
if [ -z "${EXEC_ROLE_ARN:-}" ] && present aws iam get-role --role-name "$EXEC_ROLE_NAME"; then
  # the role deploy.sh made; a role of your own (EXEC_ROLE_ARN) is left alone
  try aws iam delete-role-policy --role-name "$EXEC_ROLE_NAME" --policy-name read-the-token
  try aws iam detach-role-policy --role-name "$EXEC_ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  try aws iam delete-role --role-name "$EXEC_ROLE_NAME"
fi

echo
if [ "$FAILED" = 1 ]; then
  echo "NOT DONE: one or more steps or look-ups failed (the lines that start with FAILED). Fix the cause and run teardown.sh again." >&2
  exit 1
fi
if [ -n "${DRY:-}" ]; then echo "Nothing deleted. Run again with --yes to delete these."; else echo "done: everything deploy.sh made for $NAME is removed"; fi
