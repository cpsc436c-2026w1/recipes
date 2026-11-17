# Shared by deploy.sh, start.sh, url.sh, stop.sh and teardown.sh. Sourced, not run.
# NOT RUN BY THE COURSE YET (2026-11-13): these scripts were written from the AWS documentation and checked only
# with DRY=1 and, for the VPC and subnet look-ups, against a stand-in for aws. Read each command before you run it
# in your team sandbox.
#
# Settings (environment variables):
#   PREFIX            first part of every name, default "recipe"; each teammate sets their own (recipe-ana)
#   AWS_REGION        default ca-central-1
#   DRY=1             print every command and run none of them: nothing is looked up, created or started
#   EXEC_ROLE_ARN     an execution role you already have; unset, deploy.sh makes $PREFIX-chat-app-exec
#   ARCH              arm64 or amd64, default: the machine you build on (an image runs only on its own type)
#   COURSE_GATEWAY_URL, KB_MCP_TOKEN, CHAT_MODEL   as in the recipe README; deploy.sh needs the first two
#   (GATEWAY_URL and COURSE_MCP_TOKEN still work)
set -euo pipefail
export AWS_PAGER=""
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RECIPE=$(cd "$HERE/../.." && pwd)                 # the recipe folder: the Docker build context
export AWS_REGION=${AWS_REGION:-ca-central-1}
export AWS_DEFAULT_REGION=$AWS_REGION
PREFIX=${PREFIX:-recipe}
NAME=$PREFIX-chat-app                             # ECR repository, cluster, task family, security group
LOG_GROUP=/ecs/$NAME
SECRET_NAME=$NAME/token                           # Secrets Manager secret that holds the course MCP token
EXEC_ROLE_NAME=$NAME-exec
PORT=8000
IMAGE_TAG=${IMAGE_TAG:-$(date +%Y%m%d-%H%M)}      # every build gets its own tag; nothing is pushed as "latest"
case ${ARCH:-$(uname -m)} in
  arm64|aarch64) PLATFORM=linux/arm64; CPU_ARCH=ARM64 ;;
  *)             PLATFORM=linux/amd64; CPU_ARCH=X86_64 ;;
esac

run() { echo; echo "\$ $*"; [ -n "${DRY:-}" ] || "$@"; }
say() { echo; echo "== $*"; }
# A look-up that changes nothing. With DRY=1 it is not made and the first argument stands in for its answer.
ask() { local instead=$1; shift; if [ -n "${DRY:-}" ]; then echo "$instead"; else "$@"; fi; }

account() { ask 111122223333 aws sts get-caller-identity --query Account --output text; }
guard() {   # say which account and region the script is about to change; refuse another region
  [ "$AWS_REGION" = ca-central-1 ] || { echo "refusing: AWS_REGION is $AWS_REGION, the course runs in ca-central-1" >&2; exit 1; }
  echo "account $(account), region $AWS_REGION, names start with $NAME${DRY:+   (DRY=1: nothing is run)}"
}
# The VPC the task runs in. A course sandbox has no default VPC: its VPC is the one tagged Name=cpsc436c-vpc (seen in
# the sandbox run of 2026-11-14, README part 5). So that one is looked for first, then the account's default VPC.
# With neither, the function says so and fails, and the script that called it stops.
COURSE_VPC_NAME=${COURSE_VPC_NAME:-cpsc436c-vpc}
vpc_id() {
  [ -z "${DRY:-}" ] || { echo vpc-EXAMPLE; return 0; }
  local v
  v=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$COURSE_VPC_NAME" --query 'Vpcs[0].VpcId' --output text)
  case $v in vpc-*) ;; *) v=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text) ;; esac
  case $v in
    vpc-*) echo "$v" ;;
    *) echo "FAILED: no VPC to run in: none is tagged Name=$COURSE_VPC_NAME and the account has no default VPC" >&2; return 1 ;;
  esac
}
# A public subnet of that VPC ($1): one that gives what starts in it a public address. Fails when there is none.
public_subnet() {
  [ -z "${DRY:-}" ] || { echo subnet-EXAMPLE; return 0; }
  local s
  s=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$1" Name=map-public-ip-on-launch,Values=true \
        --query 'Subnets[0].SubnetId' --output text)
  case $s in
    subnet-*) echo "$s" ;;
    *) echo "FAILED: VPC $1 has no subnet that gives a public address (map-public-ip-on-launch)" >&2; return 1 ;;
  esac
}
sg_id() {   # $1 the VPC; without it, looked up
  local vpc=${1:-}
  [ -n "$vpc" ] || vpc=$(vpc_id) || return 1
  ask None aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$vpc" \
    --query 'SecurityGroups[0].GroupId' --output text
}
running_tasks() {
  ask "" aws ecs list-tasks --cluster "$NAME" --desired-status RUNNING --query 'taskArns[]' --output text | tr '\t' '\n' | grep -v '^None$' | grep . || true
}

# The error codes that mean "this thing does not exist", from the API reference of each look-up teardown.sh makes
# (read 2026-11-13): IAM GetRole: NoSuchEntity. ECR DescribeRepositories: RepositoryNotFoundException.
# CloudWatch Logs DescribeLogStreams and Secrets Manager DescribeSecret: ResourceNotFoundException.
# (ECS DescribeClusters and EC2 DescribeSecurityGroups with a filter do not fail for a missing thing: they return
# an empty list, so those two are read from the output, not from an error.)
GONE_CODES='NoSuchEntity|RepositoryNotFoundException|ResourceNotFoundException'
# there: does a look-up find the thing? Exit code 0: it is there. 1: it is gone (one of GONE_CODES). 2: the look-up
# itself failed, for example AccessDeniedException, so nobody knows; the error is printed. With DRY=1 nothing is
# looked up and every thing counts as there.
there() {
  [ -z "${DRY:-}" ] || return 0
  local err
  if err=$("$@" 2>&1 >/dev/null); then return 0; fi
  if echo "$err" | grep -Eq "\\(($GONE_CODES)\\)"; then return 1; fi
  echo "FAILED look-up: $*: $err" >&2
  return 2
}
# present: there, for an if. A look-up that failed counts as a failed step (FAILED=1), not as a thing that is gone.
present() {
  local rc=0
  there "$@" || rc=$?
  [ $rc -ne 2 ] || FAILED=1
  return $rc
}
