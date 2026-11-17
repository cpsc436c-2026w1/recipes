"""Run one Athena query and return its rows with the two numbers we compare: Data scanned and run time."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Result:
    rows: list[dict]
    data_scanned_bytes: int
    engine_ms: int
    query_id: str


def run(athena, query: str, workgroup: str, output: str, database: str | None = None,
        wait_s: int = 300) -> Result:
    kw = {"QueryString": query, "WorkGroup": workgroup, "ResultConfiguration": {"OutputLocation": output}}
    if database:
        kw["QueryExecutionContext"] = {"Database": database}
    qid = athena.start_query_execution(**kw)["QueryExecutionId"]
    deadline = time.time() + wait_s
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            raise TimeoutError(f"Athena query {qid} still {state} after {wait_s} s")
        time.sleep(1)
    if state != "SUCCEEDED":
        raise RuntimeError(f"Athena query {qid} {state}: {q['Status'].get('StateChangeReason')}\n{query}")
    stats = q.get("Statistics", {})
    rows: list[dict] = []
    if q.get("StatementType") == "DML":
        pages = athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid)
        header = None
        for page in pages:
            for r in page["ResultSet"]["Rows"]:
                vals = [c.get("VarCharValue") for c in r["Data"]]
                if header is None:
                    header = vals
                else:
                    rows.append(dict(zip(header, vals)))
    return Result(rows, stats.get("DataScannedInBytes", 0), stats.get("EngineExecutionTimeInMillis", 0), qid)
