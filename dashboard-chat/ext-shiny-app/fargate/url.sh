#!/bin/bash
# Print the address of the running task. Changes nothing.
source "$(dirname "$0")/common.sh"
for t in $(running_tasks); do
  ENI=$(aws ecs describe-tasks --cluster "$NAME" --tasks "$t" \
        --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
  IP=$(aws ec2 describe-network-interfaces --network-interface-ids "$ENI" --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
  echo "http://$IP:$PORT/   ($t)"
done
[ -n "$(running_tasks)" ] || echo "no task is running"
