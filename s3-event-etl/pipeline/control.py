"""When a load starts, and what a finished or failed one leaves in the run log.

The run log is the shared table `<PREFIX>-runs` (key `job`, sort key `run_id`, scheduled-merge's; made by
`ensure_run_log()`). This recipe's items:

    etl-object / <raw key>     sequencer (padded to 32), good_lines, bad_lines, repeats, checked_at, staged_key
    etl        / <start time>  run_state (running, finished, failed, skipped), failed_step, error_text, glue_run_id,
                               objects, started_at, ended_at
    etl-lock   / lock          lock_until, resumed_at

`maybe_start()` is called by the check function after it staged an object and by the hourly sweep. It starts a run
only when none is running, the last one started at least ETL_MIN_GAP_MIN minutes before, fewer than
MAX_RUNS_PER_DAY have started this UTC day, the last 3 runs did not all fail (or `etl.py resume` ran since), and it
holds the lock item.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

ETL, OBJECT, LOCK = "etl", "etl-object", "etl-lock"
OPEN_DAYS = 3                       # the run's day and the 2 days before (a file lands at most 48 h late)
HELD = "held after 3 failed runs"
GLUE_TIMEOUT_MIN = 10
# The live scenarios of R60 and R61 make a run fail on purpose, from a terminal only (never set in a function):
#   ETL_FAULT=after-merge   the job stops after its MERGE, before the manifest
#   ETL_FAULT=hang          the job sleeps until its timeout; ETL_FAULT_TIMEOUT_MIN shortens the timeout of that run
FAULTS = {"after-merge": "--FAIL_AFTER_MERGE", "hang": "--HANG"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def prefix() -> str:
    return os.environ.get("PREFIX") or "recipe"


def engine() -> str:
    e = os.environ.get("ENGINE", "glue")
    if e not in ("glue", "athena"):
        raise ValueError(f"ENGINE is glue or athena, not {e!r}")
    return e


def limits() -> tuple[int, int]:
    """(ETL_MIN_GAP_MIN, MAX_RUNS_PER_DAY): 360 and 4 with Glue, 60 and 24 with Athena, unless set."""
    gap, runs = (360, 4) if engine() == "glue" else (60, 24)
    return int(os.environ.get("ETL_MIN_GAP_MIN", gap)), int(os.environ.get("MAX_RUNS_PER_DAY", runs))


def table() -> str:
    return os.environ.get("RUNS_TABLE") or f"{prefix()}-runs"


def ddb(sess=None):
    return (sess or boto3.session.Session(region_name="ca-central-1")).client("dynamodb")


def open_days(at: datetime) -> list[str]:
    return [(at - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(OPEN_DAYS - 1, -1, -1)]


def _s(item: dict, name: str, default=None):
    v = item.get(name)
    if v is None:
        return default
    return v.get("S", v.get("N"))


def runs(client) -> list[dict]:
    """Every etl run, oldest first."""
    out, start = [], None
    while True:
        page = client.query(TableName=table(), KeyConditionExpression="job = :j", ConsistentRead=True,
                            ExpressionAttributeValues={":j": {"S": ETL}}, **({"ExclusiveStartKey": start} if start else {}))
        out += page["Items"]
        start = page.get("LastEvaluatedKey")
        if not start:
            return out


def lock_item(client) -> dict:
    return client.get_item(TableName=table(), Key={"job": {"S": LOCK}, "run_id": {"S": "lock"}},
                           ConsistentRead=True).get("Item", {})


def decide(items: list[dict], at: datetime, lock: dict) -> tuple[bool, str]:
    """(start, why) from the runs so far, without side effects."""
    gap, per_day = limits()
    real = [i for i in items if _s(i, "run_state") != "skipped"]
    if any(_s(i, "run_state") == "running" for i in real):
        return False, "a run is running"
    if real:
        last = datetime.fromisoformat(_s(real[-1], "started_at").replace("Z", "+00:00"))
        if at - last < timedelta(minutes=gap):
            return False, f"the last run started {int((at - last).total_seconds() // 60)} min ago, under the gap of " \
                          f"{gap} min: the sweep will start one"
    today = [i for i in real if _s(i, "started_at", "").startswith(at.strftime("%Y-%m-%d"))]
    if len(today) >= per_day:
        return False, f"{len(today)} runs started today, the limit is {per_day}: none until the next UTC day"
    last3 = real[-3:]
    resumed = _s(lock, "resumed_at", "")
    if len(last3) == 3 and all(_s(i, "run_state") == "failed" for i in last3) and \
            not (resumed and resumed > _s(last3[-1], "started_at")):
        return False, HELD
    return True, "start"


def take_lock(client, at: datetime) -> bool:
    try:
        client.update_item(TableName=table(), Key={"job": {"S": LOCK}, "run_id": {"S": "lock"}},
                           UpdateExpression="SET lock_until = :until",
                           ConditionExpression="attribute_not_exists(lock_until) OR lock_until < :now",
                           ExpressionAttributeValues={":until": {"S": iso(at + timedelta(minutes=15))},
                                                      ":now": {"S": iso(at)}})
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_lock(client, at: datetime) -> None:
    client.update_item(TableName=table(), Key={"job": {"S": LOCK}, "run_id": {"S": "lock"}},
                       UpdateExpression="SET lock_until = :t", ExpressionAttributeValues={":t": {"S": iso(at)}})


def record_run(client, run_id: str, state: str, **attrs) -> None:
    names = {f"#{k}": k for k in ("run_state", *attrs)}
    values = {":run_state": {"S": state}, **{f":{k}": ({"N": str(v)} if isinstance(v, (int, float)) else {"S": str(v)})
                                             for k, v in attrs.items()}}
    client.update_item(TableName=table(), Key={"job": {"S": ETL}, "run_id": {"S": run_id}},
                       UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in ("run_state", *attrs)),
                       ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def fault() -> tuple[dict, dict]:
    """(job arguments, StartJobRun options) of ETL_FAULT, or two empty dicts."""
    name = os.environ.get("ETL_FAULT", "")
    if not name:
        return {}, {}
    if name not in FAULTS:
        raise ValueError(f"ETL_FAULT is one of {', '.join(FAULTS)}, not {name!r}")
    extra = {"Timeout": int(os.environ["ETL_FAULT_TIMEOUT_MIN"])} if os.environ.get("ETL_FAULT_TIMEOUT_MIN") else {}
    return {FAULTS[name]: "true"}, extra


def maybe_start(bucket: str, sess=None, at: datetime | None = None, log=print) -> str | None:
    """Start a run if the limits allow; returns its run_id, or None."""
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    client, at = ddb(sess), at or now()
    items = runs(client)
    ok, why = decide(items, at, lock_item(client))
    if not ok:
        log(f"no start: {why}")
        if why == HELD and _s(items[-1], "error_text") != HELD:
            record_run(client, iso(at), "skipped", error_text=HELD, started_at=iso(at))
        return None
    if not take_lock(client, at):
        log("no start: another caller holds the lock")
        return None
    run_id = iso(at)
    try:
        record_run(client, run_id, "running", started_at=run_id)
        days = open_days(at)
        if engine() == "glue":
            fault_args, fault_options = fault()
            glue_run = sess.client("glue").start_job_run(
                JobName=f"{prefix()}-etl",
                Arguments={"--BUCKET": bucket, "--OPEN_DAYS": ",".join(days), "--RUN_ID": run_id,
                           "--DATABASE": prefix().replace("-", "_") + "_lake", **fault_args},
                **fault_options)["JobRunId"]
            record_run(client, run_id, "running", glue_run_id=glue_run)
            log(f"started  run {run_id}: Glue job {prefix()}-etl, run {glue_run}")
        else:
            import athena_load
            athena_load.load(sess, bucket, days, run_id, log=log)
    except ClientError as err:
        code = err.response["Error"]["Code"]
        state = "skipped" if code == "ConcurrentRunsExceededException" else "failed"
        record_run(client, run_id, state, failed_step="start", error_text=f"{code}: {err}", ended_at=iso(now()))
        log(f"{state:<8} run {run_id}: {code}")
        return None
    finally:
        release_lock(client, at)
    return run_id


def resume(sess=None, log=print) -> None:
    """After 3 failed runs: allow the next start (`etl.py resume`)."""
    ddb(sess).update_item(TableName=table(), Key={"job": {"S": LOCK}, "run_id": {"S": "lock"}},
                          UpdateExpression="SET resumed_at = :t", ExpressionAttributeValues={":t": {"S": iso(now())}})
    log("resumed: the next sweep may start a run")
