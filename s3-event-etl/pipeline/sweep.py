"""The sweep <PREFIX>-etl-sweep, every hour (schedule <PREFIX>-etl-hourly) and from `scripts/etl.py sweep`.

1. Each run recorded `running`: its state from GetJobRun. SUCCEEDED is `finished`; FAILED, TIMEOUT or ERROR is
   `failed` with failed_step (from the job's "step=<step>:" error text) and error_text, and one message to
   <PREFIX>-etl-alerts; a run running for longer than the timeout plus 5 minutes is failed the same way.
2. Each raw object of the open days whose etl-object item is missing, or has no checked_at for 15 minutes (a lost
   or failed event), is checked now.
3. When a staged object of the open days is in no manifest, control.maybe_start().
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import boto3

import check
import control
import etl_job


def topic_arn(sess) -> str:
    if os.environ.get("ALERTS_TOPIC_ARN"):
        return os.environ["ALERTS_TOPIC_ARN"]
    account = sess.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:sns:ca-central-1:{account}:{control.prefix()}-etl-alerts"


def failed(sess, run_id: str, step: str, text: str, log=print) -> None:
    control.record_run(control.ddb(sess), run_id, "failed", failed_step=step, error_text=text[:1000],
                       ended_at=control.iso(control.now()))
    sess.client("sns").publish(TopicArn=topic_arn(sess), Subject=f"{control.prefix()}-etl run {run_id} failed",
                               Message=f"ETL run {run_id} failed in step {step}: {text[:500]}\n"
                                       f"Its objects stay out of the manifests; the next run loads them.")
    log(f"failed   run {run_id}: step {step}: {text[:120]}")


def follow_runs(sess, at: datetime, log=print) -> None:
    client, glue = control.ddb(sess), sess.client("glue")
    for item in control.runs(client):
        if control._s(item, "run_state") != "running":
            continue
        run_id, glue_run = control._s(item, "run_id"), control._s(item, "glue_run_id", "")
        started = datetime.fromisoformat(control._s(item, "started_at").replace("Z", "+00:00"))
        if glue_run and not glue_run.startswith("athena:"):
            r = glue.get_job_run(JobName=f"{control.prefix()}-etl", RunId=glue_run)["JobRun"]
            state = r["JobRunState"]
            if state == "SUCCEEDED":
                control.record_run(client, run_id, "finished", ended_at=control.iso(at))
                log(f"finished run {run_id}")
                continue
            if state in ("FAILED", "TIMEOUT", "ERROR"):
                text = r.get("ErrorMessage", state)
                step = text.split("step=", 1)[1].split(":", 1)[0] if "step=" in text else "load"
                failed(sess, run_id, step, text, log)
                continue
        if at - started > timedelta(minutes=control.GLUE_TIMEOUT_MIN + 5):
            failed(sess, run_id, "load", f"running for more than {control.GLUE_TIMEOUT_MIN + 5} min", log)


def lost_objects(sess, bucket: str, days: list[str], at: datetime, log=print) -> int:
    s3, client, n = sess.client("s3"), control.ddb(sess), 0
    for day in days:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{check.RAW}dt={day}/"):
            for o in page.get("Contents", []):
                if not o["Key"].endswith(".jsonl"):
                    continue
                item = client.get_item(TableName=control.table(), ConsistentRead=True,
                                       Key={"job": {"S": control.OBJECT}, "run_id": {"S": o["Key"]}}).get("Item")
                stale = item and "checked_at" not in item and at - o["LastModified"] > timedelta(minutes=15)
                if item is None or stale:
                    # no event's sequencer: the object's ETag stands in (a later write has another ETag)
                    seq = item["sequencer"]["S"] if item else "0"
                    check.check_object(bucket, o["Key"], seq, sess, start=False, log=log)
                    n += 1
    return n


def sweep(bucket: str | None = None, sess=None, at: datetime | None = None, log=print) -> str | None:
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    at = at or control.now()
    bucket = bucket or os.environ["BUCKET"]
    follow_runs(sess, at, log)
    days = control.open_days(at)
    lost = lost_objects(sess, bucket, days, at, log)
    if lost:
        log(f"sweep    checked {lost} objects whose event was lost or failed")
    if not etl_job.waiting(sess.client("s3"), bucket, days):
        log("sweep    nothing waiting")
        return None
    return control.maybe_start(bucket, sess, at, log)


def handler(event, context=None):
    if event.get("ready"):                  # scripts/etl.py up: the function starts and reads its environment
        return {"ready": True}
    return {"started": sweep()}
