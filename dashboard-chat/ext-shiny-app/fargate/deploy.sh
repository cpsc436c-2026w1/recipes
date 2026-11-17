#!/bin/bash
# Prepare the app on Fargate in your team sandbox. It starts NOTHING: start.sh does that, and only start.sh bills
# Fargate time. Safe to run again: a step whose resource exists is skipped.
#   bash ext-shiny-app/fargate/deploy.sh            from the recipe folder, with COURSE_GATEWAY_URL and KB_MCP_TOKEN set
#   DRY=1 bash ext-shiny-app/fargate/deploy.sh      print every command, run none
# What it makes, each name starting with $PREFIX-chat-app:
#   1. a Secrets Manager secret with your course MCP token (the task reads it at start; it is in no file or image)
#   2. an execution role that may pull the image, write logs and read that one secret (skipped with EXEC_ROLE_ARN)
#   3. an ECR repository, and the image, built on this machine and pushed
#   4. a log group (kept 7 days), an ECS cluster, a security group that lets only YOUR address reach port 8000
#   5. a task definition: 0.5 vCPU, 1 GB, the image, GATEWAY_URL and CHAT_MODEL as plain environment variables
source "$(dirname "$0")/common.sh"
# The new names first; the older GATEWAY_URL and COURSE_MCP_TOKEN still work. The task gets the older names, which
# the app also reads.
GATEWAY_URL=${COURSE_GATEWAY_URL:-${GATEWAY_URL:-}}
COURSE_MCP_TOKEN=${KB_MCP_TOKEN:-${COURSE_MCP_TOKEN:-}}
: "${GATEWAY_URL:?set COURSE_GATEWAY_URL first (recipe README, Run it)}"
: "${COURSE_MCP_TOKEN:?set KB_MCP_TOKEN first: read -s KB_MCP_TOKEN && export KB_MCP_TOKEN}"
guard
# Where the task will run: the course VPC, else the default VPC, and a public subnet of it (common.sh). Looked up
# before anything is made, so an account with neither stops here and not after the image is pushed.
VPC=$(vpc_id)
public_subnet "$VPC" >/dev/null
ACCOUNT=$(account)
REGISTRY=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
IMAGE=$REGISTRY/$NAME:$IMAGE_TAG

say "1. the course MCP token, in Secrets Manager"
# The token is piped in, so it is not in the command line, in this script's output or in the shell history.
if [ -n "${DRY:-}" ]; then
  echo; echo "\$ aws secretsmanager create-secret --name $SECRET_NAME --secret-string <your token, from the environment>"
  SECRET_ARN=arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT:secret:$SECRET_NAME-EXAMPLE
else
  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
    printf %s "$COURSE_MCP_TOKEN" | aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" --secret-string file:///dev/stdin >/dev/null
  else
    printf %s "$COURSE_MCP_TOKEN" | aws secretsmanager create-secret --name "$SECRET_NAME" --secret-string file:///dev/stdin \
      --tags Key=recipe,Value=dashboard-chat >/dev/null
  fi
  SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)
  echo "secret $SECRET_NAME holds the token"
fi

say "2. the execution role"
if [ -n "${EXEC_ROLE_ARN:-}" ]; then
  echo "using your role $EXEC_ROLE_ARN; it must be allowed to read $SECRET_NAME"
else
  EXEC_ROLE_ARN=arn:aws:iam::$ACCOUNT:role/$EXEC_ROLE_NAME
  if [ -n "${DRY:-}" ] || ! aws iam get-role --role-name "$EXEC_ROLE_NAME" >/dev/null 2>&1; then
    run aws iam create-role --role-name "$EXEC_ROLE_NAME" --tags Key=recipe,Value=dashboard-chat \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      --query Role.Arn --output text
  fi
  # pull the image and write logs (AWS's managed policy for execution roles), and read this one secret, no other
  run aws iam attach-role-policy --role-name "$EXEC_ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  run aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" --policy-name read-the-token \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$SECRET_ARN\"}]}"
fi

say "3. the image"
if [ -n "${DRY:-}" ] || ! aws ecr describe-repositories --repository-names "$NAME" >/dev/null 2>&1; then
  run aws ecr create-repository --repository-name "$NAME" --tags Key=recipe,Value=dashboard-chat --query repository.repositoryUri --output text
fi
run docker build --platform "$PLATFORM" -f "$RECIPE/ext-shiny-app/Dockerfile" -t "$NAME:local" "$RECIPE"
if [ -n "${DRY:-}" ]; then
  echo; echo "\$ aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY"
else
  aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
fi
run docker tag "$NAME:local" "$IMAGE"
run docker push "$IMAGE"

say "4. logs, cluster, security group"
if [ -n "${DRY:-}" ] || [ -z "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[].logGroupName' --output text)" ]; then
  run aws logs create-log-group --log-group-name "$LOG_GROUP" --tags recipe=dashboard-chat
fi
run aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 7
run aws ecs create-cluster --cluster-name "$NAME" --tags key=recipe,value=dashboard-chat --query cluster.status --output text
SG=$(sg_id "$VPC")
if [ "$SG" = None ] || [ -z "$SG" ]; then
  # Port 8000 is opened to one address, yours: anyone who reaches the page spends your course key's budget.
  MY_IP=$(ask 203.0.113.10 curl -s https://checkip.amazonaws.com)
  run aws ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" --description "dashboard-chat recipe: the Shiny app, port $PORT" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=recipe,Value=dashboard-chat}]" --query GroupId --output text
  SG=$(ask sg-EXAMPLE aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$VPC" \
        --query 'SecurityGroups[0].GroupId' --output text)
  run aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$PORT" --cidr "$MY_IP/32"
fi

say "5. the task definition"
TD=$(mktemp); trap 'rm -f "$TD"' EXIT
export NAME EXEC_ROLE_ARN IMAGE PORT SECRET_ARN LOG_GROUP AWS_REGION GATEWAY_URL CPU_ARCH CHAT_MODEL=${CHAT_MODEL:-qwen3.6-35b-a3b}
python3 - "$TD" <<'PY'
import json, os, sys
e = os.environ
td = {
    "family": e["NAME"], "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
    "cpu": "512", "memory": "1024",   # 0.5 vCPU, 1 GB: the page and DuckDB over a small table
    "runtimePlatform": {"cpuArchitecture": e["CPU_ARCH"], "operatingSystemFamily": "LINUX"},
    "executionRoleArn": e["EXEC_ROLE_ARN"],   # no task role: the app itself makes no AWS call
    "containerDefinitions": [{
        "name": "app", "image": e["IMAGE"], "essential": True,
        "portMappings": [{"containerPort": int(e["PORT"]), "protocol": "tcp"}],
        "environment": [{"name": k, "value": e[k]} for k in ("GATEWAY_URL", "CHAT_MODEL")],
        "secrets": [{"name": "COURSE_MCP_TOKEN", "valueFrom": e["SECRET_ARN"]}],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": e["LOG_GROUP"], "awslogs-region": e["AWS_REGION"], "awslogs-stream-prefix": "app"}},
    }],
}
open(sys.argv[1], "w").write(json.dumps(td, indent=2))
PY
[ -z "${DRY:-}" ] || { echo; echo "the task definition that would be registered:"; cat "$TD"; echo; }
run aws ecs register-task-definition --cli-input-json "file://$TD" --query taskDefinition.revision --output text
echo; echo "prepared; nothing is running and nothing bills by the hour yet. Start the app: bash $HERE/start.sh"
