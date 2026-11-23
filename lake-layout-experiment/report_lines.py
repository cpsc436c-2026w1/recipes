"""Lambda's REPORT line, the one it logs after every call: duration, billed duration, memory, and an Init Duration
when the call started a new copy of the function (a cold call, L2). Billed duration includes the init phase.

  REPORT RequestId: 3f... Duration: 849.20 ms Billed Duration: 2750 ms Memory Size: 2048 MB Max Memory Used: 410 MB Init Duration: 1900.10 ms
"""

from __future__ import annotations

import re

REPORT = re.compile(r"REPORT RequestId: (?P<id>\S+)\s+Duration: (?P<d>[\d.]+) ms\s+Billed Duration: (?P<b>\d+) ms\s+"
                    r"Memory Size: (?P<m>\d+) MB\s+Max Memory Used: (?P<u>\d+) MB(?:\s+Init Duration: (?P<i>[\d.]+) ms)?")


def parse_report(text: str) -> dict | None:
    """The last REPORT line in text (a function's log tail, or a log event), or None."""
    found = list(REPORT.finditer(text or ""))
    if not found:
        return None
    m = found[-1]
    return {"request_id": m["id"], "duration_ms": float(m["d"]), "billed_duration_ms": float(m["b"]),
            "memory_mb": int(m["m"]), "max_memory_mb": int(m["u"]),
            "init_duration_ms": float(m["i"]) if m["i"] else None}


def join_by_request(calls: list[dict], reports: list[dict]) -> list[dict]:
    """Each call with the REPORT of its request id (a call with none keeps empty fields)."""
    by_id = {r["request_id"]: r for r in reports}
    return [{**c, **by_id.get(c["request_id"], {})} for c in calls]
