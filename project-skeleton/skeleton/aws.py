"""AWS pieces more than one representative uses: the bucket, IAM roles, the Athena workgroup and queries."""
import json
import os
import time

from botocore.exceptions import ClientError

from skeleton.config import REGION, TAG

RESULTS_DAYS = 7                   # S3 deletes Athena's result files after a week (ours)
SCAN_LIMIT_BYTES = 1_000_000_000   # Athena cancels any query in the workgroup that would scan more (ours: 1 GB)


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    except ClientError as e:
        # BucketAlreadyOwnedByYou: ours, from an earlier run. BucketAlreadyExists is not caught: that name belongs
        # to another account, and a run must not go on to write the lake and the exports into it.
        if e.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
    # New buckets block public access and encrypt with S3-managed keys by default; we keep both.
    # Every Athena query leaves a result file and a metadata file under athena-results/. Nobody reads them again:
    # the scripts take the rows from the API. The lake (updates/) and the exports (exports/) have no rule.
    # This call replaces the bucket's whole lifecycle configuration: a rule that someone added to this bucket by
    # hand is gone after a run. Add such a rule to the list here, not in the console.
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": "athena-results", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"},
        "Expiration": {"Days": RESULTS_DAYS}}]})


def wait_until_gone(describe, not_found: type, sleep=time.sleep, tries: int = 120, pause_s: float = 5) -> None:
    """After a clean-up a moment ago a stream or table is still being deleted and its name is "in use": call
    `describe` until it raises `not_found`. Then the name is free and the resource can be created again."""
    for _ in range(tries):
        try:
            describe()
        except not_found:
            return
        sleep(pause_s)
    raise TimeoutError(f"still being deleted after {tries * pause_s:.0f} s; run again in a few minutes")


def ensure_role(iam, name: str, service: str, policy: dict, env_var: str) -> str:
    """The role a service acts with, holding only the actions in `policy` (README, Decisions: "permissions").

    If your sandbox does not let you create roles, set `env_var` to an existing role's ARN and nothing is created.
    """
    if os.environ.get(env_var):
        return os.environ[env_var]
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=[TAG])["Role"]["Arn"]
        fresh = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        fresh = False
    iam.put_role_policy(RoleName=name, PolicyName="least-privilege", PolicyDocument=json.dumps(policy))
    if fresh:
        time.sleep(10)      # a new role takes a few seconds before other services can assume it (assumed: 10 s)
    return arn


def ensure_workgroup(athena, workgroup: str, bucket: str) -> None:
    """A workgroup that writes results to our bucket and refuses queries that would scan more than the limit."""
    config = {"ResultConfiguration": {"OutputLocation": f"s3://{bucket}/athena-results/"},
              "EnforceWorkGroupConfiguration": True,
              "BytesScannedCutoffPerQuery": SCAN_LIMIT_BYTES,
              "PublishCloudWatchMetricsEnabled": True}
    try:
        athena.create_work_group(Name=workgroup, Configuration=config, Tags=[TAG])
    except ClientError as e:
        if "already" not in str(e).lower():
            raise


def athena_query(athena, sql: str, workgroup: str, database: str, timeout_s: int = 120) -> tuple[list[dict], int]:
    """Run one query; return its rows and its Data scanned in bytes."""
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup,
                                       QueryExecutionContext={"Database": database})["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise TimeoutError(f"Athena query {qid} still {state} after {timeout_s} s")
        time.sleep(1)
    if state != "SUCCEEDED":
        raise RuntimeError(f"Athena query {state}: {q['Status'].get('StateChangeReason')}\n{sql}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    scanned = q.get("Statistics", {}).get("DataScannedInBytes", 0)
    if not rows:
        return [], scanned
    header = [c.get("VarCharValue") for c in rows[0]["Data"]]
    return [dict(zip(header, [c.get("VarCharValue") for c in r["Data"]])) for r in rows[1:]], scanned
