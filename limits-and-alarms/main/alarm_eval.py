"""Offline check of an alarm definition against EMF log lines: would CloudWatch go to ALARM?

This mirrors how a CloudWatch metric alarm decides (CloudWatch User Guide, "Alarm evaluation"): it groups the
datapoints into periods, takes the statistic per period, looks at the last EvaluationPeriods periods, and goes to
ALARM when at least DatapointsToAlarm of them breach. Missing periods count as not breaching
(TreatMissingData = notBreaching). Percentiles use the nearest-rank method; CloudWatch's own percentiles are
close to it but not always equal, so keep test data well away from the threshold.

It is a stand-in for the real evaluation, which moto does not perform. The real path is main/fire.py.
"""

from __future__ import annotations

import math

OPS = {"GreaterThanOrEqualToThreshold": lambda v, t: v >= t, "GreaterThanThreshold": lambda v, t: v > t,
       "LessThanThreshold": lambda v, t: v < t, "LessThanOrEqualToThreshold": lambda v, t: v <= t}


def metric_values(lines: list[dict], namespace: str, metric: str, dims: dict[str, str]) -> list[tuple[int, float]]:
    """(timestamp ms, value) for every EMF line that declares this metric in this namespace with these dimensions."""
    out = []
    for doc in lines:
        for d in doc["_aws"]["CloudWatchMetrics"]:
            if d["Namespace"] != namespace or metric not in {m["Name"] for m in d["Metrics"]}:
                continue
            if any(set(ds) == set(dims) and all(doc.get(k) == v for k, v in dims.items()) for ds in d["Dimensions"]):
                v = doc[metric]
                for x in (v if isinstance(v, list) else [v]):
                    out.append((doc["_aws"]["Timestamp"], float(x)))
    return out


def statistic(values: list[float], stat: str) -> float:
    if stat == "Sum":
        return sum(values)
    if stat == "Average":
        return sum(values) / len(values)
    if stat == "Maximum":
        return max(values)
    if stat == "SampleCount":
        return float(len(values))
    if stat.startswith("p"):
        ranked = sorted(values)
        return ranked[max(0, math.ceil(float(stat[1:]) / 100 * len(ranked)) - 1)]
    raise ValueError(stat)


def evaluate(spec: dict, lines: list[dict], now_ms: int) -> tuple[str, list[float | None]]:
    """Return ("ALARM" or "OK", the statistic of each evaluated period, oldest first; None where no data)."""
    dims = {d["Name"]: d["Value"] for d in spec.get("Dimensions", [])}
    stat = spec.get("ExtendedStatistic") or spec["Statistic"]
    period_ms = spec["Period"] * 1000
    pts = metric_values(lines, spec["Namespace"], spec["MetricName"], dims)
    last_end = now_ms  # a window that ends now (our simplification of how CloudWatch places its periods)
    per_period: list[float | None] = []
    for i in range(spec["EvaluationPeriods"], 0, -1):
        start = last_end - i * period_ms
        vals = [v for t, v in pts if start <= t < start + period_ms]
        per_period.append(statistic(vals, stat) if vals else None)
    op = OPS[spec["ComparisonOperator"]]
    breaching = sum(1 for v in per_period if v is not None and op(v, spec["Threshold"]))
    to_alarm = spec.get("DatapointsToAlarm", spec["EvaluationPeriods"])
    return ("ALARM" if breaching >= to_alarm else "OK"), per_period
