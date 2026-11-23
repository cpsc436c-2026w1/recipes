"""Deploy the daily check of the deployed system, its alarm, and call it by hand.

    CHECK_TARGET=kinesis:YOUR_STREAM CHECK_QUERY=dynamodb:YOUR_TABLE uv run python scripts/check.py up [--email you@example.com]
    uv run python scripts/check.py alarm                  # after the first read has put its point
    uv run python scripts/check.py invoke read [--date 2026-11-24]
    uv run python clean_up.py --yes                       # deletes all of it

`up` makes, in this order: the run log (ensure_run_log), topic <PREFIX>-check-alerts (and an email subscription
with --email: nothing is sent until the link in the first email is clicked), the function's role
<PREFIX>-check-role, log group /aws/lambda/<PREFIX>-live-check (7 days), function <PREFIX>-live-check (Python
3.12, arm64, 256 MB, timeout 120 s, asynchronous retries 0), the Scheduler's role <PREFIX>-check-scheduler-role,
schedule group <PREFIX>-simulated-runs (tagged; the Scheduler tags groups only) and its three schedules in CHECK_TZ:
<PREFIX>-check-send at CHECK_CRON, <PREFIX>-check-read CHECK_DELAY_MIN later, <PREFIX>-check-verdict at 00:15.
Set LAMBDA_ROLE_ARN and SCHEDULER_ROLE_ARN to use roles you already have; the recipe then leaves them alone.

`alarm` makes the backstop <PREFIX>-check-failed: LiveCheck/CheckPassed, Minimum, 1 hour, 26 of 26 below 1, missing
data breaching, to the same topic.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT / "sim")]
import live_check  # noqa: E402
from run_log import ensure_run_log  # noqa: E402
from tags import prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
VERDICT_CRON = "cron(15 0 * * ? *)"
REFUSED = ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation")


def names() -> dict:
    p = prefix()
    return {"function": f"{p}-live-check", "role": f"{p}-check-role", "scheduler_role": f"{p}-check-scheduler-role",
            "topic": f"{p}-check-alerts", "group": f"{p}-simulated-runs", "send": f"{p}-check-send",
            "read": f"{p}-check-read", "verdict": f"{p}-check-verdict", "alarm": f"{p}-check-failed",
            "log_group": f"/aws/lambda/{p}-live-check", "runs": f"{p}-runs"}


def settings() -> dict:
    return {"CHECK_TZ": os.environ.get("CHECK_TZ") or "America/Vancouver",
            "CHECK_CRON": os.environ.get("CHECK_CRON") or "cron(0 6 * * ? *)",
            "CHECK_DELAY_MIN": os.environ.get("CHECK_DELAY_MIN") or "90",
            "CHECK_TARGET": os.environ.get("CHECK_TARGET") or "",
            "CHECK_QUERY": os.environ.get("CHECK_QUERY") or "",
            "CHECK_EXPECTED": os.environ.get("CHECK_EXPECTED") or "20"}


def schedules(s: dict, n: dict) -> dict[str, str]:
    """Schedule name -> cron expression, all in CHECK_TZ."""
    return {n["send"]: s["CHECK_CRON"], n["read"]: live_check.read_cron(s["CHECK_CRON"], int(s["CHECK_DELAY_MIN"])),
            n["verdict"]: VERDICT_CRON}


def target_statements(target: str, account: str) -> list[dict]:
    kind, _, name = target.partition(":")
    if kind == "kinesis":
        return [{"Effect": "Allow", "Action": ["kinesis:PutRecords"],
                 "Resource": f"arn:aws:kinesis:{REGION}:{account}:stream/{name}"}]
    if kind == "sqs":
        return [{"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
                 "Resource": f"arn:aws:sqs:{REGION}:{account}:{name.rsplit('/', 1)[-1]}"}]
    if kind == "s3":
        return [{"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{name}/raw/trip-events/*"}]
    if kind == "url":
        return [{"Effect": "Allow", "Action": ["lambda:InvokeFunctionUrl"],
                 "Resource": f"arn:aws:lambda:{REGION}:{account}:function:*",
                 "Condition": {"StringEquals": {"lambda:FunctionUrlAuthType": "AWS_IAM"}}}]
    raise ValueError(f"CHECK_TARGET {target!r}: give kinesis:<stream>, sqs:<queue>, s3:<bucket> or url:<URL>")


def query_statements(query: str, account: str) -> list[dict]:
    kind, _, arg = query.partition(":")
    if kind == "dynamodb":
        return [{"Effect": "Allow", "Action": ["dynamodb:Scan"],
                 "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{arg}"}]
    if kind == "s3":
        return [{"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{arg}",
                 "Condition": {"StringLike": {"s3:prefix": ["raw/trip-events/*"]}}},
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{arg}/raw/trip-events/*"}]
    if kind == "athena":
        bucket = f"{prefix()}-lake-{account}"
        return [{"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                                               "athena:GetQueryResults"],
                 "Resource": f"arn:aws:athena:{REGION}:{account}:workgroup/{prefix()}-wg"},
                {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"],
                 "Resource": "*"},
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject",
                                               "s3:GetBucketLocation"],
                 "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]
    raise ValueError(f"CHECK_QUERY {query!r}: give dynamodb:<table>, s3:<bucket> or athena:<SQL>")


def function_policy(n: dict, account: str, s: dict) -> dict:
    """What the function may do, and nothing else: its log group, the metric, the day's item, the verdict's
    publish, the target's send and the query's read."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:{n['log_group']}:*"},
        {"Effect": "Allow", "Action": ["cloudwatch:PutMetricData"], "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "LiveCheck"}}},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{n['runs']}"},
        {"Effect": "Allow", "Action": ["sns:Publish"], "Resource": f"arn:aws:sns:{REGION}:{account}:{n['topic']}"},
        *target_statements(s["CHECK_TARGET"], account),
        *query_statements(s["CHECK_QUERY"], account)]}


def scheduler_policy(n: dict, account: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"],
         "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{n['function']}"}]}


def trust(service: str, account: str) -> dict:
    statement = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if service == "scheduler.amazonaws.com":
        statement["Condition"] = {"StringEquals": {"aws:SourceAccount": account}}
    return {"Version": "2012-10-17", "Statement": [statement]}


def package(probe_lines: list[str]) -> bytes:
    """live_check.py, replay.py and the probe deliveries: the function needs nothing but boto3."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, arc in ((HERE / "live_check.py", "live_check.py"), (ROOT / "sim" / "replay.py", "replay.py")):
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo("probe.jsonl", date_time=(2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        z.writestr(info, "".join(line + "\n" for line in probe_lines))
    return buf.getvalue()


def probe_lines() -> list[str]:
    import generate
    return generate.delivery_lines(generate.ensure("SIM03"))


def ensure_role(iam, name: str, service: str, policy: dict, account: str, log=print) -> str:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        if recorded("iam-role", name) != "created":
            raise RuntimeError(f"role {name} exists and this ledger did not create it; set the *_ROLE_ARN variable "
                               f"to use it, or pick another PREFIX")
    except ClientError as err:
        if err.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust(service, account)),
                              Tags=tags("iam"))["Role"]["Arn"]
        record("iam-role", name, "created", arn=arn, script="scripts/check.py up", clean="clean_up.py")
        log(f"created  IAM role {name}")
    iam.put_role_policy(RoleName=name, PolicyName="check", PolicyDocument=json.dumps(policy))
    return arn


def up(sess=None, email: str | None = None, log=print, sleep=time.sleep) -> dict:
    sess = sess or boto3.session.Session(region_name=REGION)
    n, s = names(), settings()
    if not s["CHECK_TARGET"] or not s["CHECK_QUERY"]:
        raise SystemExit("set CHECK_TARGET (where the probe goes) and CHECK_QUERY (how its trips are counted)")
    crons = schedules(s, n)                          # refuses a read past midnight before anything is made
    account = sess.client("sts").get_caller_identity()["Account"]
    iam, lam, logs, sns = (sess.client(x) for x in ("iam", "lambda", "logs", "sns"))
    sched = sess.client("scheduler")
    ensure_run_log(sess)
    topic = sns.create_topic(Name=n["topic"], Tags=tags("sns"))["TopicArn"]
    if recorded("sns-topic", n["topic"]) != "created":
        record("sns-topic", n["topic"], "created", arn=topic, script="scripts/check.py up", clean="clean_up.py")
    if email:
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email)
        log("subscribed the email address: click the link in the first email, or nothing is delivered")
    role = os.environ.get("LAMBDA_ROLE_ARN") or ensure_role(iam, n["role"], "lambda.amazonaws.com",
                                                            function_policy(n, account, s), account, log)
    try:
        logs.create_log_group(logGroupName=n["log_group"], tags=tags("logs"))
        record("log-group", n["log_group"], "created", script="scripts/check.py up", clean="clean_up.py")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=n["log_group"], retentionInDays=7)
    env = {"PREFIX": prefix(), "TEAM": os.environ.get("TEAM") or prefix(), "RUNS_TABLE": n["runs"],
           "TOPIC_ARN": topic, **{k: v for k, v in s.items()}}
    code = package(probe_lines())
    try:
        lam.get_function(FunctionName=n["function"])
        lam.update_function_code(FunctionName=n["function"], ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=n["function"])
        lam.update_function_configuration(FunctionName=n["function"], Environment={"Variables": env}, Role=role)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        for attempt in range(10):                    # a new role takes a few seconds to be usable by Lambda
            try:
                arn = lam.create_function(
                    FunctionName=n["function"], Runtime="python3.12", Architectures=["arm64"], MemorySize=256,
                    Timeout=120, Role=role, Handler="live_check.handler", Code={"ZipFile": code},
                    Environment={"Variables": env}, Tags=tags("lambda"))["FunctionArn"]
                break
            except ClientError as e:
                if "assume" not in str(e) or attempt == 9:
                    raise
                sleep(3)
        record("lambda-function", n["function"], "created", arn=arn, script="scripts/check.py up", clean="clean_up.py")
        log(f"created  Lambda function {n['function']}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=n["function"])
    # Decision (README, "retries 0"): a failed call is the day's result; Lambda's 2 retries would send again.
    try:
        lam.put_function_event_invoke_config(FunctionName=n["function"], MaximumRetryAttempts=0)
    except ClientError as err:
        if err.response["Error"]["Code"] not in REFUSED:
            raise
        log(f"REFUSED  PutFunctionEventInvokeConfig ({err.response['Error']['Code']}): asynchronous retries stay at 2; "
            f"a second send is harmless, the probe's ids repeat")
    function_arn = lam.get_function(FunctionName=n["function"])["Configuration"]["FunctionArn"]
    sched_role = os.environ.get("SCHEDULER_ROLE_ARN") or ensure_role(
        iam, n["scheduler_role"], "scheduler.amazonaws.com", scheduler_policy(n, account), account, log)
    group_name = n["group"]
    try:
        group = sched.create_schedule_group(Name=n["group"], Tags=tags("scheduler"))["ScheduleGroupArn"]
        record("schedule-group", n["group"], "created", arn=group, script="scripts/check.py up",
               clean="clean_up.py (deleting the group deletes its schedules)")
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in REFUSED:
            group_name = "default"
            log(f"REFUSED  CreateScheduleGroup with tags ({code}): the schedules go in group default; clean_up.py "
                f"finds them by the name prefix {prefix()}-check-")
        elif code != "ConflictException":
            raise
    phases = {n["send"]: "send", n["read"]: "read", n["verdict"]: "verdict"}
    for name, expr in crons.items():
        spec = dict(Name=name, GroupName=group_name, ScheduleExpression=expr,
                    ScheduleExpressionTimezone=s["CHECK_TZ"], FlexibleTimeWindow={"Mode": "OFF"},
                    Target={"Arn": function_arn, "RoleArn": sched_role, "Input": json.dumps({"phase": phases[name]}),
                            "RetryPolicy": {"MaximumRetryAttempts": 0}})
        for attempt in range(10):
            try:
                try:
                    sched.create_schedule(**spec)
                except ClientError as err:
                    if err.response["Error"]["Code"] != "ConflictException":
                        raise
                    sched.update_schedule(**spec)
                break
            except ClientError as err:
                if err.response["Error"]["Code"] != "ValidationException" or attempt == 9:
                    raise
                sleep(3)                             # the Scheduler cannot assume a role made seconds ago
        if group_name == "default":
            record("schedule", name, "created", script="scripts/check.py up", clean="clean_up.py (group default)")
        log(f"schedule {name}: {expr} {s['CHECK_TZ']}")
    return {"function": function_arn, "topic": topic, "schedules": crons}


def alarm(sess=None, log=print) -> None:
    sess = sess or boto3.session.Session(region_name=REGION)
    n = names()
    topic = sess.client("sns").create_topic(Name=n["topic"])["TopicArn"]   # returns the existing topic's ARN
    cw = sess.client("cloudwatch")
    cw.put_metric_alarm(
        AlarmName=n["alarm"], AlarmDescription="no live check has passed for at least 26 hours",
        Namespace="LiveCheck", MetricName="CheckPassed",
        Dimensions=[{"Name": "Team", "Value": os.environ.get("TEAM") or prefix()}],
        Statistic="Minimum", Period=3600, EvaluationPeriods=26, DatapointsToAlarm=26, Threshold=1,
        ComparisonOperator="LessThanThreshold", TreatMissingData="breaching", AlarmActions=[topic],
        Tags=tags("cloudwatch"))
    arn = cw.describe_alarms(AlarmNames=[n["alarm"]])["MetricAlarms"][0]["AlarmArn"]
    record("cloudwatch-alarm", n["alarm"], "created", arn=arn, script="scripts/check.py alarm", clean="clean_up.py",
           bills="monthly")
    log(f"created  alarm {n['alarm']} -> {n['topic']}")


def invoke(phase: str, day: str | None = None, sess=None) -> dict:
    sess = sess or boto3.session.Session(region_name=REGION)
    payload = {"phase": phase, **({"date": day} if day else {})}
    resp = sess.client("lambda").invoke(FunctionName=names()["function"], Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read() or b"null")
    if resp.get("FunctionError"):
        raise RuntimeError(f"{phase}: {body}")
    return body


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--email")
    sub.add_parser("alarm")
    i = sub.add_parser("invoke")
    i.add_argument("phase", choices=["send", "read", "verdict"])
    i.add_argument("--date")
    args = p.parse_args(argv)
    if args.cmd == "up":
        up(email=args.email)
    elif args.cmd == "alarm":
        alarm()
    else:
        print(json.dumps(invoke(args.phase, args.date), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
