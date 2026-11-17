#!/bin/bash
# Stop every running task of the app: the hourly charges end. The image, the task definition, the cluster, the log
# group, the security group, the secret and the role stay, so start.sh works again; teardown.sh removes them.
source "$(dirname "$0")/common.sh"
guard
TASKS=$(running_tasks)
for t in $TASKS; do run aws ecs stop-task --cluster "$NAME" --task "$t" --reason stop.sh --query task.lastStatus --output text; done
[ -n "${DRY:-}" ] && { echo; echo "\$ aws ecs stop-task --cluster $NAME --task <each running task>"; exit 0; }
[ -z "$TASKS" ] || aws ecs wait tasks-stopped --cluster "$NAME" --tasks $TASKS
echo "tasks still running: $(running_tasks | wc -w)"
