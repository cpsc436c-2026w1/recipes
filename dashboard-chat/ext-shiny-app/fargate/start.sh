#!/bin/bash
# Start ONE task of the app with a public address and print its URL. From here Fargate bills by the second until
# stop.sh. Refuses to start a second task.
source "$(dirname "$0")/common.sh"
guard
if [ -n "$(running_tasks)" ]; then echo "already running: $(running_tasks)"; exec bash "$HERE/url.sh"; fi
VPC=$(vpc_id); SG=$(sg_id "$VPC"); SUBNET=$(public_subnet "$VPC")   # the same VPC as deploy.sh (common.sh)
[ -z "${DRY:-}" ] || SG=sg-EXAMPLE
case $SG in sg-*) ;; *) echo "FAILED: no security group $NAME in $VPC: run deploy.sh first" >&2; exit 1 ;; esac
run aws ecs run-task --cluster "$NAME" --task-definition "$NAME" --launch-type FARGATE --count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text
[ -z "${DRY:-}" ] || exit 0
echo "waiting until the task is RUNNING (one to two minutes)"
aws ecs wait tasks-running --cluster "$NAME" --tasks $(running_tasks)
exec bash "$HERE/url.sh"
