"""The lake variant's job `<PREFIX>-dash-refresh`: one Athena query an hour, its result written into the counts table.

Started every hour by the schedule `<PREFIX>-dash-refresh` (group `<PREFIX>-dashboard-page`); timeout 120 s,
asynchronous retries 0. One run:

1. takes the lock: item job `dash-refresh`, run_id `lock` of `<PREFIX>-runs`, a conditional write that holds only
   when `lock_until` is past; if another run holds it, the run is recorded `skipped` and nothing else happens;
2. records the run (`run_id` = its start time) as `running`;
3. runs the summary query in the workgroup `<PREFIX>-wg`: the last 12 periods of `<prefix>_lake.zone_counts`, with
   the newest `requested_time` per zone of `<prefix>_lake.trips` over the same days;
4. writes one count item per zone and period (`requested`, `generation` = the run's start) and each zone's `latest`
   item, each with a conditional UpdateItem, so a run that finishes after a newer one changes neither (`kept`);
5. records `finished` (or `failed` with `failed_step` and `error_text`) and gives the lock back.

Environment: PREFIX, COUNTS_TABLE (<PREFIX>-zone-counts), RUNS_TABLE (<PREFIX>-runs), WORKGROUP (<PREFIX>-wg),
LAKE_DB (<prefix>_lake, the prefix with - as _), ZONES (48,161,230), PERIOD (1d: the period of zone_counts).
The handler uses boto3 only, so the zip holds this file and page.py (for the period arithmetic). In Lambda its two
clients are made at module scope, in the Init phase, as page.py's is (README, Decisions: cold read).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

from page import floor, parse_period, period_key, stamp, utc

JOB = "dash-refresh"
LOCK_S = 120                 # the function's timeout: a lock older than this belongs to a run that has ended
QUERY_WAIT_S = 100           # longer than this, the run stops waiting and is recorded failed (timeout 120 s)


def settings() -> dict:
    prefix = os.environ.get("PREFIX") or "recipe"
    return {"counts_table": os.environ.get("COUNTS_TABLE") or f"{prefix}-zone-counts",
            "runs_table": os.environ.get("RUNS_TABLE") or f"{prefix}-runs",
            "workgroup": os.environ.get("WORKGROUP") or f"{prefix}-wg",
            "lake_db": os.environ.get("LAKE_DB") or f"{prefix.replace('-', '_')}_lake",
            "zones": [int(z) for z in (os.environ.get("ZONES") or "48,161,230").split(",") if z.strip()],
            "period": parse_period(os.environ.get("PERIOD") or "1d")}


def summary_sql(now: datetime, cfg: dict) -> str:
    """The one query of a run: 12 days of counts and each zone's newest request over the same days."""
    period = cfg["period"]
    if period % timedelta(days=1):
        raise ValueError("the lake job reads daily (or longer) periods of zone_counts; set PERIOD to 1d or more")
    current = floor(now, period)
    first = period_key(current - 12 * period, period)
    end = period_key(current, period)
    zones = ", ".join(str(z) for z in cfg["zones"])
    db = cfg["lake_db"]
    return f"""WITH counts AS (
  SELECT pickup_zone_id, CAST(CAST(period_start AS DATE) AS VARCHAR) AS period_start, trips_requested
  FROM "{db}"."zone_counts"
  WHERE pickup_zone_id IN ({zones})
    AND CAST(period_start AS DATE) >= DATE '{first}' AND CAST(period_start AS DATE) < DATE '{end}'
), newest AS (
  SELECT pickup_zone_id, max(requested_time) AS newest_requested_time
  FROM "{db}"."trips"
  WHERE pickup_zone_id IN ({zones})
    AND requested_time >= TIMESTAMP '{first} 00:00:00' AND requested_time < TIMESTAMP '{end} 00:00:00'
  GROUP BY pickup_zone_id
)
SELECT c.pickup_zone_id, c.period_start, c.trips_requested, n.newest_requested_time
FROM counts c LEFT JOIN newest n ON c.pickup_zone_id = n.pickup_zone_id
ORDER BY c.pickup_zone_id, c.period_start"""


def athena_time(text: str) -> str:
    """Athena prints a timestamp as `2026-11-22 23:59:02.000`; the table keeps `2026-11-22T23:59:02Z`."""
    return stamp(utc(text.replace(" ", "T") if "T" not in text else text)) if text else ""


def query(athena, sql: str, cfg: dict, sleep=time.sleep, clock=time.monotonic) -> tuple[str, list[dict]]:
    """(query id, rows as dicts) of one query in the workgroup; raises RuntimeError when it does not succeed."""
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=cfg["workgroup"])["QueryExecutionId"]
    start = clock()
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] == "SUCCEEDED":
            break
        if st["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"query {qid} {st['State']}: {st.get('StateChangeReason', '')}")
        if clock() - start > QUERY_WAIT_S:
            raise RuntimeError(f"query {qid} still {st['State']} after {QUERY_WAIT_S} s")
        sleep(1)
    rows, token, header = [], None, None
    while True:
        kw = {"QueryExecutionId": qid} | ({"NextToken": token} if token else {})
        page = athena.get_query_results(**kw)
        for r in page["ResultSet"]["Rows"]:
            values = [c.get("VarCharValue", "") for c in r["Data"]]
            if header is None:
                header = values
                continue
            rows.append(dict(zip(header, values)))
        token = page.get("NextToken")
        if not token:
            return qid, rows


def write(rows: list[dict], generation: str, ddb, cfg: dict) -> dict:
    """The run's items, each with its condition; a failed condition is `kept`, not an error."""
    out = {"count_written": 0, "count_kept": 0, "latest_written": 0, "latest_kept": 0, "kept_by_zone": {}}
    newest: dict[int, str] = {}
    for r in rows:
        zone = int(r["pickup_zone_id"])
        try:
            ddb.update_item(
                TableName=cfg["counts_table"],
                Key={"zone_id": {"N": str(zone)}, "period_start": {"S": r["period_start"][:10]}},
                UpdateExpression="SET requested = :r, generation = :g",
                ConditionExpression="attribute_not_exists(generation) OR generation < :g",
                ExpressionAttributeValues={":r": {"N": str(int(r["trips_requested"]))}, ":g": {"S": generation}})
            out["count_written"] += 1
        except ddb.exceptions.ConditionalCheckFailedException:
            out["count_kept"] += 1
            out["kept_by_zone"][zone] = out["kept_by_zone"].get(zone, 0) + 1
        t = athena_time(r.get("newest_requested_time", ""))
        if t and (zone not in newest or t > newest[zone]):
            newest[zone] = t
    for zone, t in sorted(newest.items()):
        try:
            ddb.update_item(
                TableName=cfg["counts_table"], Key={"zone_id": {"N": str(zone)}, "period_start": {"S": "latest"}},
                UpdateExpression="SET newest_event_time = :t",
                ConditionExpression="attribute_not_exists(newest_event_time) OR newest_event_time < :t",
                ExpressionAttributeValues={":t": {"S": t}})
            out["latest_written"] += 1
        except ddb.exceptions.ConditionalCheckFailedException:
            out["latest_kept"] += 1
    return out


def _put_run(ddb, cfg, run_id: str, **attrs) -> None:
    item = {"job": {"S": JOB}, "run_id": {"S": run_id}}
    for k, v in attrs.items():
        item[k] = {"N": str(v)} if isinstance(v, (int, float)) else {"S": str(v)}
    ddb.put_item(TableName=cfg["runs_table"], Item=item)


def take_lock(ddb, cfg, run_id: str, now: datetime) -> bool:
    try:
        ddb.update_item(TableName=cfg["runs_table"], Key={"job": {"S": JOB}, "run_id": {"S": "lock"}},
                        UpdateExpression="SET lock_until = :until, holder = :me",
                        ConditionExpression="attribute_not_exists(lock_until) OR lock_until < :now",
                        ExpressionAttributeValues={":until": {"S": stamp(now + timedelta(seconds=LOCK_S))},
                                                   ":now": {"S": stamp(now)}, ":me": {"S": run_id}})
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


def give_back_lock(ddb, cfg, run_id: str) -> None:
    try:
        ddb.delete_item(TableName=cfg["runs_table"], Key={"job": {"S": JOB}, "run_id": {"S": "lock"}},
                        ConditionExpression="holder = :me", ExpressionAttributeValues={":me": {"S": run_id}})
    except ddb.exceptions.ConditionalCheckFailedException:
        pass                      # the lock ran out and another run holds it now: it is that run's


def run(now: datetime, athena, ddb, cfg: dict | None = None, clock=datetime.now, sleep=time.sleep) -> dict:
    """One run of the job at `now`; returns what it recorded."""
    cfg = cfg or settings()
    run_id = stamp(now)
    if not take_lock(ddb, cfg, run_id, now):
        _put_run(ddb, cfg, run_id, run_state="skipped", started_at=run_id, ended_at=run_id)
        return {"run_id": run_id, "run_state": "skipped"}
    _put_run(ddb, cfg, run_id, run_state="running", started_at=run_id)
    step = "query"
    try:
        qid, rows = query(athena, summary_sql(now, cfg), cfg, sleep=sleep)
        step = "write"
        done = write(rows, run_id, ddb, cfg)
        ended = stamp(clock(timezone.utc))
        _put_run(ddb, cfg, run_id, run_state="finished", started_at=run_id, ended_at=ended, summary_query_id=qid,
                 rows_read=len(rows), count_written=done["count_written"], count_kept=done["count_kept"],
                 latest_written=done["latest_written"], latest_kept=done["latest_kept"])
        return {"run_id": run_id, "run_state": "finished", "query_id": qid, **done}
    except Exception as err:      # recorded, then raised: the page keeps the old values, which turn stale
        _put_run(ddb, cfg, run_id, run_state="failed", started_at=run_id, ended_at=stamp(clock(timezone.utc)),
                 failed_step=step, error_text=f"{type(err).__name__}: {err}"[:1000])
        raise
    finally:
        give_back_lock(ddb, cfg, run_id)


_clients: dict = {}


def client(service: str):
    """One client of each service per execution environment."""
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):      # set by Lambda: make the clients in the Init phase
    client("athena"), client("dynamodb")


def lambda_handler(event, context=None):
    out = run(datetime.now(timezone.utc), client("athena"), client("dynamodb"))
    print(json.dumps({"request_id": getattr(context, "aws_request_id", None), **{k: v for k, v in out.items()
                                                                                 if k != "kept_by_zone"}}))
    return {k: v for k, v in out.items() if k != "kept_by_zone"}
