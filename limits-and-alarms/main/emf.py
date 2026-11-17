"""CloudWatch embedded metric format (EMF): one JSON log line per request carries its metrics.

In Lambda, a line printed to standard output goes to CloudWatch Logs, and CloudWatch Logs turns the fields the
`_aws` block names into metrics. No PutMetricData call, no extra latency in the request, and the same line is a
searchable log record (README, "Decisions": how metrics leave the app).

Specification: CloudWatch User Guide, "Embedded metric format specification".
"""

from __future__ import annotations

import json
import sys
import time

METRICS = [("Requests", "Count"), ("Errors", "Count"), ("LatencyMs", "Milliseconds"), ("CostCAD", "None"),
           ("TokensIn", "Count"), ("TokensOut", "Count")]


def request_line(namespace: str, service: str, *, latency_ms: float, error: bool, cost_cad: float,
                 tokens_in: int = 0, tokens_out: int = 0, request_id: str = "", route: str = "",
                 ts_ms: int | None = None, extra: dict | None = None) -> dict:
    """The EMF document for one request."""
    return {
        "_aws": {
            "Timestamp": int(ts_ms if ts_ms is not None else time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                # One dimension, the service. The request id stays a plain field: a dimension with a new value per
                # request creates a new custom metric per request, each one billed (README, "Decisions").
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": n, "Unit": u} for n, u in METRICS],
            }],
        },
        "Service": service,
        "Requests": 1,
        "Errors": 1 if error else 0,
        "LatencyMs": round(float(latency_ms), 1),
        "CostCAD": float(cost_cad),
        "TokensIn": int(tokens_in),
        "TokensOut": int(tokens_out),
        "requestId": request_id,
        "route": route,
        **(extra or {}),
    }


def emit(doc: dict, out=None) -> None:
    """Print the document as one line: no text before or after it, or CloudWatch does not extract the metrics."""
    print(json.dumps(doc, separators=(",", ":")), file=out or sys.stdout, flush=True)
