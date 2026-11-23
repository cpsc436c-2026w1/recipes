"""The daily check of the deployed system: Lambda function <PREFIX>-live-check, called by three schedules.

    {"phase": "send"}      at CHECK_CRON: send the probe SIM03 (60 events, 20 trips), ids probe-<local date>-...
    {"phase": "read"}      CHECK_DELAY_MIN later: count that day's probe trips with CHECK_QUERY, write the day's item
                           in the run log (job live-check, run_id = the local date), put 1 or 0 to LiveCheck/CheckPassed
    {"phase": "verdict"}   at 00:15: if a send was due on the local date that has just ended and its item is missing
                           or not finished, publish one message to <PREFIX>-check-alerts
    add "date": "YYYY-MM-DD" to read or judge another date (the tests and the live demonstration do)

Settings (environment of the function): CHECK_TZ (default America/Vancouver), CHECK_CRON (default
cron(0 6 * * ? *)), CHECK_DELAY_MIN (default 90), CHECK_TARGET (`kinesis:<stream>`, `sqs:<queue>`, `url:<function
URL>`, `s3:<bucket>`, `local:<module:function>`), CHECK_QUERY (`dynamodb:<table>`: distinct trip_id starting with the
day's probe prefix; `athena:<SQL>` run in <PREFIX>-wg, `{prefix}` replaced, first cell of the first row; `s3:<bucket>`:
distinct trip_id in that day's probe objects under raw/trip-events/), CHECK_EXPECTED (default 20), RUNS_TABLE,
TOPIC_ARN, TEAM.

The function carries this file, sim/replay.py and probe.jsonl (SIM03's deliveries) and needs only boto3, which the
Lambda Python runtime has. Decision (README, "two schedules"): the read is its own call, so no call waits.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

import replay

REGION = "ca-central-1"
HERE = Path(__file__).resolve().parent
JOB = "live-check"
DAYS = {"SUN": 1, "MON": 2, "TUE": 3, "WED": 4, "THU": 5, "FRI": 6, "SAT": 7}
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"), 1)}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def tz() -> ZoneInfo:
    return ZoneInfo(setting("CHECK_TZ", "America/Vancouver"))


def cron_fields(expr: str) -> list[str]:
    """The six fields of a Scheduler cron expression: minutes hours day-of-month month day-of-week year."""
    m = re.fullmatch(r"cron\((.+)\)", expr.strip())
    fields = (m.group(1) if m else expr).split()
    if len(fields) != 6:
        raise ValueError(f"{expr}: a Scheduler cron expression has 6 fields, cron(minutes hours day-of-month month "
                         f"day-of-week year)")
    return fields


def _matches(field: str, value: int, names: dict | None = None) -> bool:
    if field in ("*", "?"):
        return True
    for part in field.split(","):
        part = part.upper()
        for name, number in (names or {}).items():
            part = part.replace(name, str(number))
        if re.search(r"[LW#/]", part):
            raise ValueError(f"cron field {field}: L, W, # and / are not handled here; list the days instead")
        low, _, high = part.partition("-")
        if int(low) <= value <= int(high or low):
            return True
    return False


def due_on(expr: str, day: date) -> bool:
    """Whether the cron expression fires on that local date (once or more)."""
    _, _, dom, month, dow, year = cron_fields(expr)
    aws_dow = day.isoweekday() % 7 + 1           # AWS cron: 1 = Sunday ... 7 = Saturday
    return (_matches(dom, day.day) and _matches(month, day.month, MONTHS) and _matches(dow, aws_dow, DAYS)
            and _matches(year, day.year))


def read_cron(send_expr: str, delay_min: int) -> str:
    """The read schedule: the send's time plus the delay, on the same days. The read must land before midnight, so
    that the day's verdict at 00:15 finds the day's item."""
    minute, hour, dom, month, dow, year = cron_fields(send_expr)
    if not (minute.isdigit() and hour.isdigit()):
        raise ValueError(f"{send_expr}: give one minute and one hour, so the read time can be computed")
    total = int(hour) * 60 + int(minute) + delay_min
    if total >= 24 * 60:
        raise ValueError(f"the send at {int(hour):02d}:{int(minute):02d} plus CHECK_DELAY_MIN {delay_min} is past "
                         f"midnight; send earlier in the day")
    return f"cron({total % 60} {total // 60} {dom} {month} {dow} {year})"


def local_day(t: datetime) -> date:
    return t.astimezone(tz()).date()


def probe_prefix(day: date) -> str:
    return f"probe-{day.isoformat()}"


def _session():
    return boto3.session.Session(region_name=REGION)


def probe_deliveries() -> list[dict]:
    """SIM03's deliveries: probe.jsonl in the function's package, or the delivery files of sim/data/SIM03/ when run
    from the folder."""
    path = HERE / "probe.jsonl"
    if path.exists():
        lines = path.read_text().splitlines()
    else:
        import generate                 # offline and in the tests: the generator writes SIM03 if it is missing
        lines = generate.delivery_lines(generate.ensure("SIM03"))
    return [json.loads(line) for line in lines if line.strip()]


def send(now: datetime, sess=None) -> dict:
    """Send SIM03 under the run id probe-<local date>, its times moved to now."""
    kind, _, target = setting("CHECK_TARGET").partition(":")
    deliveries = probe_deliveries()
    run_id = probe_prefix(local_day(now))
    sender = replay.make_sender(kind, target or None, run_id=run_id, session=sess or _session())
    return replay.replay(deliveries, sender, run_id, 0, shift_to=now)


def count_probe_trips(day: date, sess=None) -> int:
    """The day's probe trips in the deployed system, by CHECK_QUERY."""
    sess = sess or _session()
    kind, _, arg = setting("CHECK_QUERY").partition(":")
    pre = probe_prefix(day) + "-"
    if kind == "dynamodb":
        ddb, trips, kw = sess.client("dynamodb"), set(), {}
        while True:              # Decision (README, "probe count"): a filtered scan, fine for a table of a demo
            page = ddb.scan(TableName=arg, ProjectionExpression="trip_id",
                            FilterExpression="begins_with(trip_id, :p)", ExpressionAttributeValues={":p": {"S": pre}},
                            **kw)
            trips |= {i["trip_id"]["S"] for i in page["Items"]}
            if "LastEvaluatedKey" not in page:
                return len(trips)
            kw = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    if kind == "s3":
        s3, trips = sess.client("s3"), set()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=arg, Prefix="raw/trip-events/"):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(f"-{probe_prefix(day)}.jsonl"):
                    body = s3.get_object(Bucket=arg, Key=obj["Key"])["Body"].read().decode()
                    trips |= {json.loads(line)["trip_id"] for line in body.splitlines() if line.strip()}
        return len(trips)
    if kind == "athena":
        athena = sess.client("athena")
        qid = athena.start_query_execution(QueryString=arg.replace("{prefix}", pre),
                                           WorkGroup=f"{setting('PREFIX', 'recipe')}-wg")["QueryExecutionId"]
        for _ in range(50):
            state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if state["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(2)
        if state["State"] != "SUCCEEDED":
            raise RuntimeError(f"Athena query {qid} {state['State']}: {state.get('StateChangeReason', '')}")
        rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
        return int(rows[1]["Data"][0]["VarCharValue"])
    if kind == "local":                 # offline: the stand-in pipeline's table
        from rides.pipeline import stored_events
        return len({e["trip_id"] for e in stored_events() if e["trip_id"].startswith(pre)})
    raise ValueError(f"CHECK_QUERY {setting('CHECK_QUERY')!r}: give dynamodb:<table>, s3:<bucket> or athena:<SQL>")


def read(now: datetime, day: date | None = None, sess=None) -> dict:
    """Count the day's probe trips; write the day's item and the metric point."""
    sess = sess or _session()
    day = day or local_day(now - timedelta(minutes=int(setting("CHECK_DELAY_MIN", "90"))))
    expected = int(setting("CHECK_EXPECTED", "20"))
    item = {"job": JOB, "run_id": day.isoformat(), "started_at": replay.iso(now)}
    try:
        n = count_probe_trips(day, sess)
        item.update(run_state="finished" if n == expected else "failed", checked_count=n, error_text="")
    except Exception as err:            # every failure of the read is the day's result, not a crash
        item.update(run_state="failed", checked_count=-1, error_text=f"{type(err).__name__}: {err}"[:500])
    item["ended_at"] = replay.iso(datetime.now(timezone.utc))
    ddb = sess.client("dynamodb")
    ddb.put_item(TableName=setting("RUNS_TABLE"), Item={
        k: {"N": str(v)} if isinstance(v, int) else {"S": v} for k, v in item.items()})
    sess.client("cloudwatch").put_metric_data(Namespace="LiveCheck", MetricData=[{
        "MetricName": "CheckPassed", "Value": 1 if item["run_state"] == "finished" else 0, "Unit": "Count",
        "Dimensions": [{"Name": "Team", "Value": setting("TEAM", setting("PREFIX", "recipe"))}]}])
    return item


def verdict_message(day: date, item: dict | None, expected: int = 20) -> str | None:
    """What the verdict publishes for a day whose send was due, or None when the check passed."""
    if item is None:
        return f"{day.isoformat()}: no check"
    if item.get("run_state") == "finished":
        return None
    if item.get("error_text"):
        return f"{day.isoformat()}: {item['error_text']}"
    return f"{day.isoformat()}: {item.get('checked_count')} probe trips, not {expected}"


def verdict(now: datetime, day: date | None = None, sess=None) -> dict:
    """Judge the local date that has just ended (the schedule fires at 00:15)."""
    sess = sess or _session()
    day = day or local_day(now) - timedelta(days=1)
    if not due_on(setting("CHECK_CRON", "cron(0 6 * * ? *)"), day):
        return {"date": day.isoformat(), "due": False, "published": None}
    got = sess.client("dynamodb").get_item(TableName=setting("RUNS_TABLE"), ConsistentRead=True,
                                           Key={"job": {"S": JOB}, "run_id": {"S": day.isoformat()}}).get("Item")
    item = {k: int(v["N"]) if "N" in v else v.get("S") for k, v in got.items()} if got else None
    message = verdict_message(day, item, int(setting("CHECK_EXPECTED", "20")))
    published = None
    if message:
        published = sess.client("sns").publish(TopicArn=setting("TOPIC_ARN"), Subject="Live check failed"[:100],
                                               Message=message)["MessageId"]
    return {"date": day.isoformat(), "due": True, "message": message, "published": published}


def handler(event, context=None, now: datetime | None = None, sess=None):
    now = now or datetime.now(timezone.utc)
    phase = event.get("phase")
    day = date.fromisoformat(event["date"]) if event.get("date") else None
    if phase == "send":
        result = send(now, sess)
    elif phase == "read":
        result = read(now, day, sess)
    elif phase == "verdict":
        result = verdict(now, day, sess)
    else:
        raise ValueError(f"phase must be send, read or verdict, not {phase!r}")
    print(json.dumps({"phase": phase, **result}, default=str))
    return result
