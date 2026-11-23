"""Lambda function <PREFIX>-chat-query: run one query the chat sends, on Athena, within the limits.

Its execution role is <PREFIX>-chat-reader, which may start queries only in <PREFIX>-chat-wg and read only the
tables trips and zones (README, "The reader role"). The parse check lives in the chat (main/sqlcheck.py), so this
function runs what it is sent; the role and the workgroup are what stop the rest (R170).

Event: {"sql": "...", "deadline": <epoch seconds>, "workgroup": "<optional>"}. The function

1. reserves 100 MB in today's item of <PREFIX>-chat-usage with one conditional UpdateItem; when the day's billed
   Data scanned would pass 5 GB, it answers "daily scan limit reached" and starts nothing (R200);
2. starts the query in the workgroup, with the database <prefix>_chat as the context (the model names tables
   without a database);
3. polls GetQueryExecution every 0.5 s until the query ends, or until the smaller of the deadline and 10 s from its
   start; then it stops the query with StopQueryExecution (R210);
4. reads at most 50 rows with GetQueryResults, drops the header row, casts each value by its column type;
5. settles the reservation with what the query billed (R180) and returns the state, rows, Data scanned and billed
   bytes.

The package holds this file and settings.py; boto3 comes with the Python 3.12 runtime.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from settings import (DAILY_CAP_MB, DAILY_LIMIT_RESULT, MB, POLL_S, QUERY_MAX_S, RESERVE_MB, SCAN_LIMIT_RESULT,
                      billed_mb)

MAX_ROWS = 50  # the limits of dashboard-chat's engine (main/engine.py)
MAX_CELL_CHARS = 200
USAGE_TTL_DAYS = 3
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
# The workgroup's per-query cutoff ends a query as CANCELLED with the reason "Bytes scanned limit was exceeded" and
# DataScannedInBytes 104857600, the cutoff itself (measured in the live R150 run, 2026-11-23). The reason text is
# matched in either FAILED or CANCELLED, and billed for what was scanned, or the daily cap would under-count.
CUTOFF_REASON = "bytes scanned limit"


class RealClock:
    """Epoch seconds; Lambda and the laptop agree on it, so the chat's deadline means the same here."""

    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(max(0.0, s))


def handler(event, context=None):
    import boto3

    region = os.environ.get("AWS_REGION", "ca-central-1")
    return run_query(event, athena=boto3.client("athena", region_name=region),
                     usage=boto3.client("dynamodb", region_name=region), env=os.environ, clock=RealClock())


def run_query(event: dict, *, athena, usage, env, clock, poll_s: float = POLL_S,
              query_max_s: float = QUERY_MAX_S) -> dict:
    sql = event["sql"]
    deadline = float(event.get("deadline") or clock.now() + query_max_s)
    workgroup = event.get("workgroup") or env["WORKGROUP"]
    table = env["USAGE_TABLE"]
    day = datetime.fromtimestamp(clock.now(), timezone.utc).strftime("%Y-%m-%d")
    if not _reserve(usage, table, day, clock):
        return _result("REFUSED", message=DAILY_LIMIT_RESULT)
    try:
        qid = athena.start_query_execution(
            QueryString=sql, WorkGroup=workgroup,
            QueryExecutionContext={"Database": env["DATABASE"]})["QueryExecutionId"]
    except Exception as e:  # the role refused (AccessDeniedException), or Athena refused the SQL at once
        _settle(usage, table, day, 0)
        return _result("DENIED" if _denied(e) else "FAILED", message=_first_line(e))
    start = clock.now()
    wait_until = min(deadline, start + query_max_s)
    stopped = False
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state in TERMINAL:
            break
        if clock.now() >= wait_until:
            # Decision (one deadline): a query still running when its wait ends is stopped, so it bills no more.
            athena.stop_query_execution(QueryExecutionId=qid)
            stopped = True
            q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state not in TERMINAL:
                state = "CANCELLED"
            break
        clock.sleep(min(poll_s, max(0.0, wait_until - clock.now())))
    reason = q["Status"].get("StateChangeReason", "") or ""
    scanned = int((q.get("Statistics") or {}).get("DataScannedInBytes") or 0)
    at_cutoff = CUTOFF_REASON in reason.lower()
    billed = billed_mb(state, scanned, at_cutoff) * MB
    _settle(usage, table, day, billed)
    common = dict(query_execution_id=qid, data_scanned_bytes=scanned, billed_bytes=billed,
                  workgroup=q.get("WorkGroup", workgroup),
                  database=(q.get("QueryExecutionContext") or {}).get("Database", env["DATABASE"]))
    if at_cutoff:
        return _result("CUTOFF", message=SCAN_LIMIT_RESULT, **common)
    if state == "FAILED" and _denied_text(reason):
        # The role's Glue or S3 refusals come back as a FAILED query, not as an error of StartQueryExecution
        # (measured in the live R170 run: DROP TABLE fails in Hive's DDLTask, CTAS with "Insufficient permissions").
        return _result("DENIED", message="access denied: " + reason.splitlines()[0][:200], **common)
    if stopped:
        at_deadline = clock.now() >= deadline - 1e-6
        return _result("STOPPED", message="deadline" if at_deadline else f"query stopped after {query_max_s:g} s",
                       **common)
    if state != "SUCCEEDED":
        return _result(state, message=reason.splitlines()[0][:200] if reason else state, **common)
    columns, types, rows, truncated = _rows(athena, qid)
    return _result("SUCCEEDED", columns=columns, types=types, rows=rows, truncated=truncated, **common)


def _result(state: str, *, message: str = "", columns=(), types=(), rows=(), truncated=None,
            query_execution_id: str = "", data_scanned_bytes: int = 0, billed_bytes: int = 0, workgroup: str = "",
            database: str = "") -> dict:
    return {"state": state, "message": message, "columns": list(columns), "types": list(types), "rows": list(rows),
            "truncated": truncated, "query_execution_id": query_execution_id,
            "data_scanned_bytes": data_scanned_bytes, "billed_bytes": billed_bytes, "workgroup": workgroup,
            "database": database}


def _reserve(usage, table: str, day: str, clock) -> bool:
    """ADD the reserve to today's billed bytes, only if the day is still at or below the cap minus the reserve."""
    reserve = RESERVE_MB * MB
    limit = (DAILY_CAP_MB - RESERVE_MB) * MB
    expires = int(clock.now()) + USAGE_TTL_DAYS * 86400
    try:
        usage.update_item(
            TableName=table, Key={"usage_date": {"S": day}},
            UpdateExpression="ADD billed_bytes :reserve SET expires_at = :expires",
            ConditionExpression="attribute_not_exists(billed_bytes) OR billed_bytes <= :limit",
            ExpressionAttributeValues={":reserve": {"N": str(reserve)}, ":limit": {"N": str(limit)},
                                       ":expires": {"N": str(expires)}})
        return True
    except Exception as e:
        if type(e).__name__ == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in str(e):
            return False
        raise


def _settle(usage, table: str, day: str, billed_bytes: int) -> None:
    """Give back the reserve and add what was billed: one ADD of the difference (a negative number subtracts)."""
    delta = int(billed_bytes) - RESERVE_MB * MB
    usage.update_item(TableName=table, Key={"usage_date": {"S": day}}, UpdateExpression="ADD billed_bytes :delta",
                      ExpressionAttributeValues={":delta": {"N": str(delta)}})


def _rows(athena, qid: str):
    """At most MAX_ROWS rows, typed; the first row of a SELECT's results is the header and is dropped."""
    page = athena.get_query_results(QueryExecutionId=qid, MaxResults=MAX_ROWS + 2)
    info = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    columns = [c["Name"] for c in info]
    types = [c["Type"].lower() for c in info]
    raw = page["ResultSet"]["Rows"]
    if raw and [d.get("VarCharValue") for d in raw[0]["Data"]] == columns:
        raw = raw[1:]  # assumed: the header row comes first (README, "Open")
    cut = ["rows"] if len(raw) > MAX_ROWS or page.get("NextToken") else []
    rows = [[cast(d.get("VarCharValue"), t) for d, t in zip(r["Data"], types)] for r in raw[:MAX_ROWS]]
    if any(isinstance(v, str) and len(v) > MAX_CELL_CHARS for r in rows for v in r):
        cut.append("text")
    rows = [[v[:MAX_CELL_CHARS] if isinstance(v, str) else v for v in r] for r in rows]
    return columns, types, rows, " and ".join(cut) or None


def cast(value: str | None, athena_type: str):
    """One value of GetQueryResults (always text) as the Python value its column type names."""
    if value is None:
        return None
    t = athena_type.lower()
    if t in ("tinyint", "smallint", "integer", "int", "bigint"):
        return int(value)
    if t in ("double", "float", "real"):
        return float(value)
    if t.startswith("decimal"):
        return float(Decimal(value))  # a float, so the tool result is plain JSON
    if t == "boolean":
        return value.lower() == "true"
    return value  # varchar, date, timestamp: kept as Athena writes them


def _denied(e: Exception) -> bool:
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
    return code in ("AccessDeniedException", "AccessDenied") or _denied_text(str(e))


def _denied_text(text: str) -> bool:
    """A refusal by IAM in an error or a query's StateChangeReason (the live R170 reasons name the role and say
    "is not authorized to perform" or "Insufficient permissions")."""
    t = (text or "").lower()
    return "not authorized" in t or "insufficient permissions" in t or "accessdenied" in t or "access denied" in t


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:200]

