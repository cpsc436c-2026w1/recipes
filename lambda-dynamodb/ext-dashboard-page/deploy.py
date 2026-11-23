"""Deploy the page (and, with --lake, its hourly job), and open or close its public URL. run.sh calls this file.

    uv run python ext-dashboard-page/deploy.py up            # counts table, page function, roles, schedule group
    uv run python ext-dashboard-page/deploy.py up --lake     # also the hourly lake job, its role and schedule
    uv run python ext-dashboard-page/deploy.py url-on 3      # a one-time schedule that deletes the URL in 3 h, then the URL
    uv run python ext-dashboard-page/deploy.py url-off       # the URL and its schedule, now
    DRY=1 ...                                                # prints every call, makes none
    MEMORY_MB=1024 ... up                                    # the page's memory (default 512; the job keeps 128)

Every create carries the course tags of scripts/tags.py and is written to the ledger state/ledger.jsonl; the
recipe's clean_up.py deletes what the ledger lists. A resource that is there already is kept and used.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
sys.path[:0] = [str(RECIPE / "scripts"), str(HERE)]
from tags import prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
LOG_DAYS = int(os.environ.get("LOG_DAYS") or 7)
ROLE_TRIES, ROLE_WAIT_S = 6, float(os.environ.get("ROLE_WAIT") or 5)
PAGE_MEMORY_MB = 512          # the page's memory unless MEMORY_MB says otherwise (README, Decisions: cold read)
JOB_MEMORY_MB = 128


def page_memory() -> int:
    mb = int(os.environ.get("MEMORY_MB") or PAGE_MEMORY_MB)
    if not 128 <= mb <= 10240:
        raise SystemExit(f"MEMORY_MB must be 128 to 10240, not {mb}")
    return mb


def names() -> dict:
    p = prefix()
    return {"page": f"{p}-page", "page_role": f"{p}-page-role", "job": f"{p}-dash-refresh",
            "job_role": f"{p}-dash-refresh-role", "scheduler_role": f"{p}-dash-scheduler-role",
            "url_role": f"{p}-dash-url-role", "group": f"{p}-dashboard-page", "hourly": f"{p}-dash-refresh",
            "url_off": f"{p}-url-off", "counts": os.environ.get("COUNTS_TABLE") or f"{p}-zone-counts",
            "runs": f"{p}-runs", "wg": f"{p}-wg", "lake_db": f"{p.replace('-', '_')}_lake",
            "slots": os.environ.get("SLOTS_TABLE") or ""}


class Aws:
    """Each call printed before it is made; with dry=True printed only (`DRY=1`)."""

    def __init__(self, sess, dry: bool, log=print):
        self.sess, self.dry, self.log = sess, dry, log
        self._clients = {}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self.sess.client(service) if self.sess else None
        return self._clients[service]

    def __call__(self, service: str, op: str, show: str = "", **kw):
        self.log(f"$ aws {service} {op.replace('_', '-')} {show}".rstrip())
        if self.dry:
            return {}
        return getattr(self.client(service), op)(**kw)

    def account(self) -> str:
        return "<account>" if self.dry else self.client("sts").get_caller_identity()["Account"]


def rec(aws: "Aws", *args, **kw) -> None:
    """A ledger line, except in a dry run (nothing was made)."""
    if not aws.dry:
        record(*args, **kw)


def code(err) -> str:
    return getattr(err, "response", {}).get("Error", {}).get("Code", "")


def zip_of(*files: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(HERE / f, f)
    return buf.getvalue()


# ---- roles

def trust(service: str) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]})


def role(aws: Aws, name: str, service: str, policy: dict) -> str:
    """Create the role if missing (recorded `created`), then put its one inline policy. Returns its ARN."""
    account = aws.account()
    arn = f"arn:aws:iam::{account}:role/{name}"
    try:
        aws("iam", "create_role", name, RoleName=name, AssumeRolePolicyDocument=trust(service), Tags=tags("iam"))
        rec(aws, "iam-role", name, "created", arn=arn, script="ext-dashboard-page/deploy.py", clean="clean_up.py")
    except Exception as err:
        if code(err) != "EntityAlreadyExists":
            raise
        aws.log("  (there already: kept)")
        if recorded("iam-role", name) is None:
            rec(aws, "iam-role", name, "borrowed", arn=arn, script="ext-dashboard-page/deploy.py")
    aws("iam", "put_role_policy", f"{name} policy", RoleName=name, PolicyName="dashboard-page",
        PolicyDocument=json.dumps(policy))
    return arn


def logs_statement(account: str, fn: str) -> dict:
    return {"Sid": "OwnLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{fn}:*"}


def page_policy(account: str, n: dict) -> dict:
    tables = [f"arn:aws:dynamodb:{REGION}:{account}:table/{n['counts']}"]
    if n["slots"]:
        tables.append(f"arn:aws:dynamodb:{REGION}:{account}:table/{n['slots']}")
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadCounts", "Effect": "Allow", "Action": ["dynamodb:Query"], "Resource": tables},
        logs_statement(account, n["page"])]}


def job_policy(account: str, n: dict) -> dict:
    bucket = f"{prefix()}-lake-{account}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Query", "Effect": "Allow",
         "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"],
         "Resource": f"arn:aws:athena:{REGION}:{account}:workgroup/{n['wg']}"},
        {"Sid": "Catalog", "Effect": "Allow",
         "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"],
         "Resource": [f"arn:aws:glue:{REGION}:{account}:catalog",
                      f"arn:aws:glue:{REGION}:{account}:database/{n['lake_db']}",
                      f"arn:aws:glue:{REGION}:{account}:table/{n['lake_db']}/trips",
                      f"arn:aws:glue:{REGION}:{account}:table/{n['lake_db']}/zone_counts"]},
        {"Sid": "ReadTables", "Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": [f"arn:aws:s3:::{bucket}/lake/trips/*", f"arn:aws:s3:::{bucket}/lake/zone_counts/*",
                      f"arn:aws:s3:::{bucket}/athena-results/*"]},
        {"Sid": "Results", "Effect": "Allow", "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
         "Resource": f"arn:aws:s3:::{bucket}/athena-results/*"},
        {"Sid": "Bucket", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{bucket}"},
        {"Sid": "WriteCounts", "Effect": "Allow", "Action": ["dynamodb:UpdateItem"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{n['counts']}"},
        {"Sid": "RunLog", "Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{n['runs']}"},
        logs_statement(account, n["job"])]}


# ---- functions

def log_group(aws: Aws, fn: str) -> None:
    group = f"/aws/lambda/{fn}"
    try:
        aws("logs", "create_log_group", group, logGroupName=group, tags=tags("logs"))
        rec(aws, "log-group", group, "created", script="ext-dashboard-page/deploy.py", clean="clean_up.py")
    except Exception as err:
        if code(err) != "ResourceAlreadyExistsException":
            raise
        aws.log("  (there already: kept)")
    aws("logs", "put_retention_policy", f"{group} {LOG_DAYS} days", logGroupName=group, retentionInDays=LOG_DAYS)


def function(aws: Aws, name: str, role_arn: str, handler: str, files: tuple, timeout: int, env: dict,
             sleep=time.sleep, memory: int = JOB_MEMORY_MB) -> None:
    """Create the function (Python 3.12, arm64, `memory` MB), or replace the code and settings of one that is there."""
    blob = b"" if aws.dry else zip_of(*files)
    try:
        aws("lambda", "get_function", name, FunctionName=name)
        exists = not aws.dry
    except Exception as err:
        if code(err) != "ResourceNotFoundException":
            raise
        exists = False
    if exists:
        aws("lambda", "update_function_code", name, FunctionName=name, ZipFile=blob)
        aws.client("lambda").get_waiter("function_updated_v2").wait(FunctionName=name)
        aws("lambda", "update_function_configuration", f"{name} environment, {memory} MB", FunctionName=name,
            Environment={"Variables": env}, Timeout=timeout, MemorySize=memory)
        aws.client("lambda").get_waiter("function_updated_v2").wait(FunctionName=name)
        return
    for attempt in range(1, ROLE_TRIES + 1):        # a new role takes a few seconds before Lambda can use it
        try:
            aws("lambda", "create_function",
                f"{name} python3.12 arm64 {memory} MB {timeout} s ({', '.join(files)})", FunctionName=name,
                Runtime="python3.12", Architectures=["arm64"], MemorySize=memory, Timeout=timeout, Role=role_arn,
                Handler=handler, Code={"ZipFile": blob}, Environment={"Variables": env}, Tags=tags("lambda"))
            break
        except Exception as err:
            if code(err) != "InvalidParameterValueException" or attempt == ROLE_TRIES:
                raise
            aws.log(f"  (the role is not usable yet: trying again in {ROLE_WAIT_S:g} s, attempt {attempt + 1})")
            sleep(ROLE_WAIT_S)
    account = aws.account()
    rec(aws, "lambda-function", name, "created", arn=f"arn:aws:lambda:{REGION}:{account}:function:{name}",
           script="ext-dashboard-page/deploy.py", clean="clean_up.py")
    if not aws.dry:
        aws.client("lambda").get_waiter("function_active_v2").wait(FunctionName=name)


def schedule_group(aws: Aws, n: dict) -> None:
    try:
        aws("scheduler", "create_schedule_group", n["group"], Name=n["group"], Tags=tags("scheduler"))
        rec(aws, "schedule-group", n["group"], "created", script="ext-dashboard-page/deploy.py", clean="clean_up.py")
    except Exception as err:
        if code(err) != "ConflictException":
            raise
        aws.log("  (there already: kept)")


def page_env(n: dict) -> dict:
    env = {"PREFIX": prefix(), "COUNTS_TABLE": n["counts"]}
    for k in ("ZONES", "PERIOD", "STALE_MINUTES", "SLOTS_TABLE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def up(aws: Aws, lake: bool, sleep=time.sleep) -> None:
    n, account = names(), aws.account()
    aws.log("== 1. The counts table (created if missing; one your counting function writes is used as it is)")
    if aws.dry:
        aws.log(f"$ aws dynamodb create-table {n['counts']} (if missing) zone_id N, period_start S, on demand")
    else:
        from load_counts import ensure_table
        ensure_table(aws.sess, aws.log)
    aws.log("== 2. The page's role: Query on the counts table (and the slot table), its own log group")
    page_role = role(aws, n["page_role"], "lambda.amazonaws.com", page_policy(account, n))
    aws.log("== 3. The page's log group, then the function")
    log_group(aws, n["page"])
    function(aws, n["page"], page_role, "page.lambda_handler", ("page.py", "page.html", "slots.py"), 10,
             page_env(n), sleep, memory=page_memory())
    aws.log("== 4. The schedule group (tagged) and the role the URL's one-time schedule runs as")
    schedule_group(aws, n)
    role(aws, n["url_role"], "scheduler.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:DeleteFunctionUrlConfig",
         "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{n['page']}"}]})
    if not lake:
        aws.log("== Done: the page is deployed without a public URL. Show it locally with serve_local.py, or "
                "publicly with: bash ext-dashboard-page/run.sh url-on 3")
        return
    aws.log("== 5. Lake variant: the workgroup and the run log (each created if missing)")
    if aws.dry:
        aws.log(f"$ aws athena create-work-group {n['wg']} (if missing: ensure_workgroup of scripts/lake.py)")
        aws.log(f"$ aws dynamodb create-table {n['runs']} (if missing: ensure_run_log of scripts/run_log.py)")
    else:
        from lake import ensure_workgroup
        from run_log import ensure_run_log
        ensure_workgroup(n["wg"], sess=aws.sess)
        ensure_run_log(aws.sess)
    aws.log("== 6. The job's role, log group and function: 120 s, no asynchronous retries")
    job_role = role(aws, n["job_role"], "lambda.amazonaws.com", job_policy(account, n))
    log_group(aws, n["job"])
    env = {"PREFIX": prefix(), "COUNTS_TABLE": n["counts"], "RUNS_TABLE": n["runs"], "WORKGROUP": n["wg"],
           "LAKE_DB": n["lake_db"], "PERIOD": os.environ.get("PERIOD") or "1d"}
    if os.environ.get("ZONES"):
        env["ZONES"] = os.environ["ZONES"]
    function(aws, n["job"], job_role, "refresh.lambda_handler", ("refresh.py", "page.py"), 120, env, sleep)
    aws("lambda", "put_function_event_invoke_config", f"{n['job']} retries 0", FunctionName=n["job"],
        MaximumRetryAttempts=0)
    aws.log("== 7. The hourly schedule, and the role it runs as")
    sched_role = role(aws, n["scheduler_role"], "scheduler.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction",
         "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{n['job']}"}]})
    target = {"Arn": f"arn:aws:lambda:{REGION}:{account}:function:{n['job']}", "RoleArn": sched_role,
              "RetryPolicy": {"MaximumRetryAttempts": 0}}
    try:
        aws("scheduler", "create_schedule", f"{n['hourly']} rate(1 hour) in {n['group']}", Name=n["hourly"],
            GroupName=n["group"], ScheduleExpression="rate(1 hour)", FlexibleTimeWindow={"Mode": "OFF"},
            Target=target)
    except Exception as err:
        if code(err) != "ConflictException":
            raise
        aws.log("  (there already: kept)")


# ---- the public URL

def url_on(aws: Aws, hours: float, now: datetime) -> int:
    n, account = names(), aws.account()
    at = (now + timedelta(hours=hours)).astimezone(timezone.utc).replace(microsecond=0)
    aws.log(f"== 1. First the one-time schedule that deletes the URL at {at:%Y-%m-%dT%H:%M:%S} UTC")
    try:
        aws("scheduler", "create_schedule", f"{n['url_off']} at({at:%Y-%m-%dT%H:%M:%S}) in {n['group']}",
            Name=n["url_off"], GroupName=n["group"], ScheduleExpression=f"at({at:%Y-%m-%dT%H:%M:%S})",
            ScheduleExpressionTimezone="UTC", FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="DELETE",
            Target={"Arn": "arn:aws:scheduler:::aws-sdk:lambda:deleteFunctionUrlConfig",
                    "RoleArn": f"arn:aws:iam::{account}:role/{n['url_role']}",
                    "Input": json.dumps({"FunctionName": n["page"]}), "RetryPolicy": {"MaximumRetryAttempts": 3}})
    except Exception as err:
        aws.log(f"FAILED: the schedule that would delete the URL was not made ({code(err) or type(err).__name__}: "
                f"{err}); no URL was created")
        return 1
    aws.log("== 2. Then the URL (auth type NONE) and the two statements a public URL needs")
    try:
        try:
            aws("lambda", "create_function_url_config", f"{n['page']} NONE", FunctionName=n["page"], AuthType="NONE")
        except Exception as err:
            if code(err) != "ResourceConflictException":
                raise
            aws.log("  (there already: kept)")
        for sid, kw in (("url-public", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                        ("url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True})):
            try:
                aws("lambda", "add_permission", f"{n['page']} {sid}", FunctionName=n["page"], StatementId=sid,
                    Principal="*", **kw)
            except Exception as err:
                if code(err) != "ResourceConflictException":
                    raise
        rec(aws, "lambda-url", n["page"], "created", script="ext-dashboard-page/deploy.py url-on",
               clean="run.sh url-off, its schedule, or clean_up.py")
    except Exception as err:
        aws.log(f"FAILED: the URL was not made ({code(err) or type(err).__name__}: {err}); deleting its schedule")
        try:
            aws("scheduler", "delete_schedule", n["url_off"], Name=n["url_off"], GroupName=n["group"])
        except Exception as err2:
            aws.log(f"FAILED: the schedule {n['url_off']} was not deleted either: {err2}")
        return 1
    if not aws.dry:
        url = aws.client("lambda").get_function_url_config(FunctionName=n["page"])["FunctionUrl"]
        aws.log(f"The page is public until {at:%Y-%m-%dT%H:%M:%S} UTC: {url}")
        aws.log("End it earlier with: bash ext-dashboard-page/run.sh url-off")
    return 0


def url_off(aws: Aws) -> int:
    n, failed = names(), 0
    for what, service, op, kw in (
            ("URL", "lambda", "delete_function_url_config", {"FunctionName": n["page"]}),
            ("schedule", "scheduler", "delete_schedule", {"Name": n["url_off"], "GroupName": n["group"]}),
            ("statement url-public", "lambda", "remove_permission", {"FunctionName": n["page"], "StatementId": "url-public"}),
            ("statement url-invoke", "lambda", "remove_permission", {"FunctionName": n["page"], "StatementId": "url-invoke"})):
        try:
            aws(service, op, " ".join(str(v) for v in kw.values()), **kw)
        except Exception as err:
            if code(err) == "ResourceNotFoundException":
                aws.log(f"  (no {what}: already gone)")
            else:
                aws.log(f"FAILED: the {what} was not deleted: {err}")
                failed = 1
    if not failed:
        rec(aws, "lambda-url", n["page"], "deleted", script="ext-dashboard-page/deploy.py url-off")
    return failed


def main(argv=None, sess=None, now: datetime | None = None, log=print, sleep=time.sleep) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--lake", action="store_true", help="also the hourly job that reads the lake with Athena")
    on = sub.add_parser("url-on")
    on.add_argument("hours", type=float, help="how long the URL stays up, in hours (a decimal is fine: 0.1)")
    sub.add_parser("url-off")
    a = p.parse_args(argv)
    dry = bool(os.environ.get("DRY"))
    if sess is None and not dry:
        import boto3
        sess = boto3.session.Session(region_name=REGION)
    aws = Aws(sess, dry, log)
    if a.cmd == "up":
        up(aws, a.lake, sleep)
        return 0
    if a.cmd == "url-on":
        if a.hours <= 0:
            p.error("hours must be more than 0")
        return url_on(aws, a.hours, now or datetime.now(timezone.utc))
    return url_off(aws)


if __name__ == "__main__":
    sys.exit(main())
