"""Lag of a run: read what the consumer stored and report the median and p95 of each part of the lag."""


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least p percent of the values at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))           # ceil(n * p / 100), at least 1
    return ordered[int(rank) - 1]


def scan_all(ddb, table: str) -> list[dict]:
    """Every stored update as plain numbers. A Scan is fine for one run's few hundred items, not for a live table."""
    items, kwargs = [], {"TableName": table, "ConsistentRead": True}   # every write that succeeded, as wait_stored
    while True:
        page = ddb.scan(**kwargs)
        for it in page["Items"]:
            items.append({
                "update_id": it["update_id"]["S"],
                "event_ms": int(it["event_ms"]["N"]),
                "entered_ms": int(it["entered_ms"]["N"]),
                "arrival_ms": int(it["arrival_ms"]["N"]),
            })
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def summarize(items: list[dict]) -> dict:
    """Median and p95, in seconds, of the whole lag and of its two parts."""
    parts = {
        "lag: event to stored": [i["arrival_ms"] - i["event_ms"] for i in items],
        "event to entered (before the broker)": [i["entered_ms"] - i["event_ms"] for i in items],
        "entered to stored (broker and consumer)": [i["arrival_ms"] - i["entered_ms"] for i in items],
    }
    out = {"stored": len(items)}
    if not items:
        return out                                      # nothing stored: the report says so, and has no lag to show
    for name, ms in parts.items():
        out[name] = {"median_s": percentile(ms, 50) / 1000, "p95_s": percentile(ms, 95) / 1000}
    return out


def report(summary: dict) -> str:
    lines = [f"updates stored: {summary['stored']}", "",
             f"{'part of the lag':42} {'median s':>9} {'p95 s':>9}"]
    for name, v in summary.items():
        if isinstance(v, dict):
            lines.append(f"{name:42} {v['median_s']:9.3f} {v['p95_s']:9.3f}")
    return "\n".join(lines)
