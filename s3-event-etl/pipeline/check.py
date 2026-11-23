"""The check function <PREFIX>-etl-check: S3 calls it for each object created under raw/trip-events/ ending .jsonl.

For each record of the event:
1. claim the object in the run log (job etl-object, run_id = the key) with one conditional PutItem: no item yet, or
   its sequencer is smaller (a later write of the key), or it has no checked_at (an earlier try stopped before the
   end). A failed condition is a repeat of an object already checked: add 1 to its repeats and stop.
2. check each line against the delivery model; write the good lines, unchanged and in order, to
   stage/trip-events/dt=<day>/<stem>-<sequencer>.jsonl, the others as {"line_no", "reason", "line"} to
   quarantine/trip-events-rows/dt=<day>/<stem>-<sequencer>.jsonl, both with If-None-Match: * (a retry finds them).
3. record good_lines, bad_lines and checked_at, then ask for a run (control.maybe_start) when ENGINE is glue;
   with ENGINE=athena the hourly sweep starts the load.

It writes only under stage/ and quarantine/, never under the prefix it watches, so it cannot call itself.
"""
from __future__ import annotations

import os
import urllib.parse

import boto3
from botocore.exceptions import ClientError

import control
from rides.events import DELIVERY, LineError, validate_line

RAW = "raw/trip-events/"
STAGE = "stage/trip-events/"
QUARANTINE = "quarantine/trip-events-rows/"


def reason(err: LineError) -> str:
    """The reason a line was set aside, in words."""
    text = str(err).split(": ", 1)[-1]
    if "Extra inputs" in text:
        return f"extra field {err.field}"
    if "Field required" in text:
        return f"{err.field} missing"
    if "timezone" in text or "time zone" in text:
        return f"{err.field} has no time zone"
    return f"{err.field}: {text}"


def names(key: str, sequencer: str) -> tuple[str, str, str]:
    """(day, staged key, quarantine key) of a raw key: dt=<day> from its folder."""
    rest = key[len(RAW):]
    folder, _, file = rest.rpartition("/")
    day = folder.removeprefix("dt=") if folder.startswith("dt=") else "unknown"
    stem = file.removesuffix(".jsonl")
    return day, f"{STAGE}dt={day}/{stem}-{sequencer}.jsonl", f"{QUARANTINE}dt={day}/{stem}-{sequencer}.jsonl"


def claim(ddb, key: str, sequencer: str) -> bool:
    try:
        ddb.put_item(TableName=control.table(), Item={"job": {"S": control.OBJECT}, "run_id": {"S": key},
                                                      "sequencer": {"S": sequencer}, "repeats": {"N": "0"}},
                     ConditionExpression="attribute_not_exists(run_id) OR sequencer < :s OR "
                                         "attribute_not_exists(checked_at)",
                     ExpressionAttributeValues={":s": {"S": sequencer}})
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        ddb.update_item(TableName=control.table(), Key={"job": {"S": control.OBJECT}, "run_id": {"S": key}},
                        UpdateExpression="ADD repeats :one", ExpressionAttributeValues={":one": {"N": "1"}})
        return False


def put_once(s3, bucket: str, key: str, body: str) -> None:
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), IfNoneMatch="*")
    except ClientError as err:
        if err.response["Error"]["Code"] != "PreconditionFailed":     # a retry: it is there already
            raise


def check_object(bucket: str, key: str, sequencer: str, sess=None, start: bool = True, log=print) -> dict:
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    s3, ddb = sess.client("s3"), sess.client("dynamodb")
    sequencer = sequencer.zfill(32)
    if not claim(ddb, key, sequencer):
        log(f"repeat   {key}")
        return {"key": key, "repeat": True}
    text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    good, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            validate_line(line, DELIVERY, key, n)
            good.append(line)
        except LineError as err:
            bad.append({"line_no": n, "reason": reason(err), "line": line})
    day, staged, quarantined = names(key, sequencer)
    import json
    if good:
        put_once(s3, bucket, staged, "".join(g + "\n" for g in good))
    if bad:
        put_once(s3, bucket, quarantined, "".join(json.dumps(b, sort_keys=True) + "\n" for b in bad))
    ddb.update_item(TableName=control.table(), Key={"job": {"S": control.OBJECT}, "run_id": {"S": key}},
                    UpdateExpression="SET good_lines = :g, bad_lines = :b, checked_at = :t, staged_key = :k",
                    ExpressionAttributeValues={":g": {"N": str(len(good))}, ":b": {"N": str(len(bad))},
                                               ":t": {"S": control.iso(control.now())},
                                               ":k": {"S": staged if good else "-"}})
    log(f"checked  {key}: {len(good)} good, {len(bad)} bad")
    if good and start and control.engine() == "glue":
        # with ENGINE=athena the sweep loads (R80): the check function's role has no Athena actions, and a start
        # from here was recorded as a failed run (live run, 2026-11-23), which counts towards the hold of R61
        control.maybe_start(bucket, sess, log=log)
    return {"key": key, "good": len(good), "bad": len(bad), "staged": staged if good else None}


def handler(event, context=None):
    if event.get("ready"):                  # scripts/etl.py up: the function starts and reads its environment
        return {"ready": True}
    out = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        if key.startswith(RAW) and key.endswith(".jsonl"):
            out.append(check_object(bucket, key, rec["s3"]["object"]["sequencer"],
                                    start=os.environ.get("START_ON_STAGE", "1") == "1"))
    return out
