"""The REPORT line Lambda writes after every call, parsed, and fetched from the function's log group by request id.

    REPORT RequestId: 3b93f44a-...\tDuration: 316.02 ms\tBilled Duration: 810 ms\tMemory Size: 128 MB\t
        Max Memory Used: 92 MB\tInit Duration: 493.18 ms

`Init Duration` is there only when the call started a new execution environment: such a call is `cold`
(L2; Lambda developer guide, execution environment lifecycle). Other recipes copy this file to read REPORT lines.
"""
from __future__ import annotations

import re
import time

LINE = re.compile(
    r"REPORT RequestId:\s*(?P<request_id>[0-9A-Za-z-]+)\s+Duration:\s*(?P<duration_ms>[\d.]+)\s*ms\s+"
    r"Billed Duration:\s*(?P<billed_ms>\d+)\s*ms\s+Memory Size:\s*(?P<memory_mb>\d+)\s*MB\s+"
    r"Max Memory Used:\s*(?P<max_memory_mb>\d+)\s*MB(?:\s+Init Duration:\s*(?P<init_ms>[\d.]+)\s*ms)?")


def parse(message: str) -> dict | None:
    """The fields of one REPORT line, or None for any other log line."""
    m = LINE.search(message)
    if not m:
        return None
    d = m.groupdict()
    return {"request_id": d["request_id"], "duration_ms": float(d["duration_ms"]), "billed_ms": int(d["billed_ms"]),
            "memory_mb": int(d["memory_mb"]), "max_memory_mb": int(d["max_memory_mb"]),
            "init_ms": float(d["init_ms"]) if d["init_ms"] else None}


def call_class(report: dict | None) -> str:
    """`cold` with an Init Duration, `warm` without; `unknown` when no REPORT line was found for the call."""
    if report is None:
        return "unknown"
    return "cold" if report["init_ms"] is not None else "warm"


def gb_seconds(billed_ms: float, memory_mb: float) -> float:
    """What Lambda bills for duration: the billed time in seconds times the configured memory in GB (2^30 bytes)."""
    return billed_ms / 1000 * memory_mb / 1024


def fetch(logs, group: str, start_ms: int, end_ms: int, want: set[str], wait_s: float = 90,
          sleep=time.sleep, clock=time.monotonic) -> dict[str, dict]:
    """REPORT lines of `want` (request ids) logged between start_ms and end_ms. Log lines can take seconds to
    arrive, so it asks again (every 5 s, at most wait_s) until every id is found; missing ids are left out."""
    found: dict[str, dict] = {}
    begun = clock()
    while True:
        kw = {"logGroupName": group, "startTime": start_ms, "endTime": end_ms,
              "filterPattern": '"REPORT RequestId"'}
        while True:
            page = logs.filter_log_events(**kw)
            for event in page.get("events", []):
                r = parse(event["message"])
                if r and r["request_id"] in want:
                    found[r["request_id"]] = r
            if not page.get("nextToken"):
                break
            kw["nextToken"] = page["nextToken"]
        if want <= set(found) or clock() - begun >= wait_s:
            return found
        sleep(5)
