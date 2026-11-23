"""Data latency of a run of ext-queue-ingest, what it used, and the 30-day and year cost of that use.

    uv run python ext-queue-ingest/latency.py --run-id <run id>      # about 15 min after run.py --target aws
    uv run python ext-queue-ingest/latency.py --table <no-window run id> <30 s run id> <60 s run id> \
        --idle <PREFIX of each, or - where not measured> > ../output/batching-window.txt

Latency of a trip = the item's `stored_time` minus the `sent_time` of the line of evidence/<run id>/sent.jsonl with
the item's `delivery_id` (the trips service's send to the queue, to the item stored). A completion of the data
set's answer from _truth/ without an item counts as over the target. The p95 is by the nearest rank: the ceil(0.95 n)-th
smallest of the n completions (the 796th of 837). Only this run's items count (their trip ids start with the run
id), so a second run into the same table is measured on its own.

What the run used, each measured: the calls and their batch sizes from the consumer's own line per call, billed
milliseconds from Lambda's REPORT lines, the bytes the log group took in (IncomingBytes), the write attempts, the
messages left in the dead-letter queue, and the SQS requests on the queue counted from CloudTrail data events
(request_count.py: CloudWatch counts messages and empty receives, not the receives that returned messages). Without
the trail's counts the requests are printed as a lower bound and the cost says so.

Cost: 30 days (and a year) of traffic like this run's, back to back: the run's cost x 720 (8,760) / run hours, the
run being from its first send to its last item stored. It writes evidence/<run id>/measured.json; `--table` puts
two of them side by side, the table a slide quotes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_S = {30: 60.0, 60: 90.0}       # window -> target p95 (the window plus 30 s)

# Price sheet, ca-central-1, 2026-11-18 (USD); free allowances left out (the free tier is the organisation's).
PRICE = {
    "sqs_request": 0.40 / 1e6,          # standard queue, per request (a batch of up to 10 is one request)
    "lambda_request": 0.20 / 1e6,
    "lambda_gb_s": 0.0000133334,        # arm64
    "ddb_write": 0.6875 / 1e6,          # on demand, per write unit
    "logs_gb": 0.55,                    # CloudWatch Logs ingestion
}
CAD = 1.45                              # the course's planning rate
LEASE_USD = 30.0


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def nearest_rank(values: list[float], q: float = 0.95) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def latencies(items: list[dict], sent: list[dict], completions: list[str], run_id: str) -> dict:
    """Seconds from send to stored for each completion of the answer from _truth/; a missing one is infinite (over the target).
    `items` are the table's items (trip_id, delivery_id, stored_time); `completions` the truth's trip ids."""
    sent_at = {line["delivery_id"]: parse(line["sent_time"]) for line in sent if line.get("result", "ok") == "ok"}
    by_trip = {i["trip_id"].removeprefix(f"{run_id}-"): i for i in items}
    out = {}
    for trip in completions:
        item = by_trip.get(trip)
        if item is None or item["delivery_id"] not in sent_at:
            out[trip] = math.inf
        else:
            out[trip] = (parse(item["stored_time"]) - sent_at[item["delivery_id"]]).total_seconds()
    return out


def costs(units: dict, run_hours: float) -> dict:
    """USD of the run and of 30 days and a year of traffic like the run's, back to back (720 / run_hours runs a
    month): every unit was measured in the run, so nothing here assumes a batch size, a duration or a poll rate.

    units: sqs_requests (every billed call on the queue: sends, receives with and without messages, deletes, the
    poller's attribute reads), invocations, billed_ms (summed), memory_mb, writes (conditional puts, repeats
    included: a refused conditional put is billed a write unit too), log_bytes (ingested)."""
    parts = {
        "sqs": units["sqs_requests"] * PRICE["sqs_request"],
        "lambda": units["invocations"] * PRICE["lambda_request"]
        + units["billed_ms"] / 1000 * units["memory_mb"] / 1024 * PRICE["lambda_gb_s"],
        "dynamodb": units["writes"] * PRICE["ddb_write"],
        "logs": units["log_bytes"] / 2 ** 30 * PRICE["logs_gb"],
    }
    run = sum(parts.values())
    month, year = run * 720 / run_hours, run * 8760 / run_hours
    return {"parts": parts, "run_usd": run, "month_usd": month, "month_cad": month * CAD,
            "year_usd": year, "year_cad": year * CAD, "lease_pct": 100 * month / LEASE_USD,
            "year_lease_pct": 100 * year / LEASE_USD}


def batch_lines(messages: list[str]) -> dict:
    """The consumer's per-call lines ({"batch": n, ...}) and Lambda's REPORT lines, read from the log group:
    calls, batch sizes (mean, median), billed and run milliseconds, the write attempts (stored + repeat)."""
    sizes, billed, ran, writes = [], [], [], 0
    for m in messages:
        m = m.strip()
        if m.startswith("{") and '"batch"' in m:
            row = json.loads(m)
            sizes.append(row["batch"])
            writes += row.get("stored", 0) + row.get("repeat", 0)
        elif m.startswith("REPORT "):
            fields = dict(f.split(": ", 1) for f in m[len("REPORT "):].split("\t") if ": " in f)
            billed.append(float(fields["Billed Duration"].split()[0]))
            ran.append(float(fields["Duration"].split()[0]))
    return {"calls": len(sizes), "sizes": sizes, "mean_batch": statistics.fmean(sizes) if sizes else 0.0,
            "median_batch": statistics.median(sizes) if sizes else 0.0, "billed_ms": sum(billed),
            "mean_ms": statistics.fmean(ran) if ran else 0.0, "median_ms": statistics.median(ran) if ran else 0.0,
            "reports": len(billed), "writes": writes}


def log_messages(logs, group: str, start: datetime, end: datetime) -> list[str]:
    out, kw = [], {"logGroupName": group, "startTime": int(start.timestamp() * 1000),
                   "endTime": int(end.timestamp() * 1000)}
    while True:
        page = logs.filter_log_events(**kw)
        out += [e["message"] for e in page["events"]]
        if not page.get("nextToken"):
            return out
        kw["nextToken"] = page["nextToken"]


def metric_sum(cw, namespace: str, name: str, dims: dict, start: datetime, end: datetime) -> float:
    resp = cw.get_metric_statistics(Namespace=namespace, MetricName=name, StartTime=start, EndTime=end, Period=60,
                                    Statistics=["Sum"], Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()])
    return sum(p["Sum"] for p in resp["Datapoints"])


def scan(ddb, table: str) -> list[dict]:
    found, kw = [], {"TableName": table, "ConsistentRead": True}
    while True:
        page = ddb.scan(**kw)
        found += [{k: next(iter(v.values())) for k, v in it.items()} for it in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return found
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def report(lat: dict, window: int, printed=print) -> bool:
    values = list(lat.values())
    for trip, s in sorted(lat.items()):
        printed(f"{trip} {s:.3f}" if math.isfinite(s) else f"{trip} not stored")
    p95 = nearest_rank(values)
    target = TARGET_S.get(window, window + 30.0)
    ok = p95 < target
    printed(f"{len(values)} latencies; p95 {p95:.1f} s (nearest rank {math.ceil(0.95 * len(values))} of "
            f"{len(values)}); target under {target:.0f} s at a {window} s window: {'met' if ok else 'NOT met'}")
    return ok


def mapping_settings(lam, function: str) -> dict:
    found = lam.list_event_source_mappings(FunctionName=function)["EventSourceMappings"]
    m = found[0] if found else {}
    return {"window_s": m.get("MaximumBatchingWindowInSeconds", 0), "batch_size": m.get("BatchSize", 0),
            "max_copies": (m.get("ScalingConfig") or {}).get("MaximumConcurrency")}


def dlq_depth(sqs, name: str) -> int:
    url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                               "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def queue_requests(counts: dict, queue: str) -> dict:
    """The billed calls on one queue from request_count.py's counts ("<queue> <action>": n)."""
    mine = {k.split(" ", 1)[1]: v for k, v in counts.items() if k.split(" ", 1)[0] == queue}
    return {"by_action": mine, "total": sum(mine.values())}


def usable(counted: dict | None, send_calls: int, warn=lambda m: print(m, file=sys.stderr)) -> dict | None:
    """The trail's check on itself: the sender knows its own calls. A trail that began logging after the run began
    (logging starts a few minutes after StartLogging) or files not delivered yet give fewer; such counts are not
    used, and the requests fall back to a lower bound."""
    if counted and counted["by_action"].get("SendMessageBatch", 0) != send_calls:
        warn(f"CloudTrail counted {counted['by_action'].get('SendMessageBatch', 0)} SendMessageBatch calls, the "
             f"sender made {send_calls}: the counts are incomplete and are not used")
        return None
    return counted


def trail_counts(session, trail_prefix: str, start: datetime, end: datetime, count=None) -> dict | None:
    """request_count.py's counts over the run's own window (first send - 1 min to last stored + 1 min), so idle
    polls after the run and the set-up's calls before it are left out; None when there is no trail bucket."""
    if count is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import request_count

        count = request_count.count
    try:
        return count(session, trail_prefix, start, end)
    except Exception as err:   # noqa: BLE001  no trail (a lease may refuse CloudTrail): the requests fall back to a bound
        print(f"no CloudTrail counts ({type(err).__name__}): SQS requests are a lower bound", file=sys.stderr)
        return None


def measure(session, prefix: str, run_id: str, data_set: str, requests: dict | None,
            trail_prefix: str | None = None) -> dict:
    sys.path.insert(0, str(ROOT / "sim"))
    import stand_in

    evidence = ROOT / "evidence" / run_id
    sent = [json.loads(x) for x in (evidence / "sent.jsonl").read_text().splitlines() if x.strip()]
    truth = __import__("truth").answer(stand_in.ensure(data_set))
    items = [i for i in scan(session.client("dynamodb"), f"{prefix}-trips-completed")
             if i["trip_id"].startswith(f"{run_id}-")]
    lat = latencies(items, sent, sorted(truth["fare_by_trip"]), run_id)
    first = min(parse(x["sent_time"]) for x in sent)
    last = max([parse(i["stored_time"]) for i in items] + [parse(x["sent_time"]) for x in sent])
    start, end = first - timedelta(minutes=1), last + timedelta(minutes=1)
    queue, fn = f"{prefix}-trip-completed", f"{prefix}-trip-completed-consumer"
    cw = session.client("cloudwatch")
    sqs_metrics = {name: int(metric_sum(cw, "AWS/SQS", name, {"QueueName": queue}, start, end))
                   for name in ("NumberOfMessagesSent", "NumberOfMessagesReceived", "NumberOfMessagesDeleted",
                                "NumberOfEmptyReceives")}
    log_bytes = int(metric_sum(cw, "AWS/Logs", "IncomingBytes", {"LogGroupName": f"/aws/lambda/{fn}"}, start, end))
    calls = batch_lines(log_messages(session.client("logs"), f"/aws/lambda/{fn}", start, end))
    summary = json.loads((evidence / "summary.json").read_text())
    if requests is None and trail_prefix:
        requests = trail_counts(session, trail_prefix, start, end)
    counted = queue_requests(requests, queue) if requests else None
    counted = usable(counted, summary["calls"])
    if counted:
        sqs_requests, basis = counted["total"], "counted (CloudTrail data events)"
    else:
        # at least: every send call, one receive per 10 messages received, one delete per 10 deleted, the empties
        sqs_requests = (summary["calls"] + math.ceil(sqs_metrics["NumberOfMessagesReceived"] / 10)
                        + math.ceil(sqs_metrics["NumberOfMessagesDeleted"] / 10) + sqs_metrics["NumberOfEmptyReceives"])
        basis = "lower bound (no CloudTrail counts): receives and deletes of 10 messages each"
    finite = sorted(v for v in lat.values() if math.isfinite(v))
    run_hours = (last - first).total_seconds() / 3600
    units = {"sqs_requests": sqs_requests, "invocations": calls["calls"], "billed_ms": calls["billed_ms"],
             "memory_mb": 128, "writes": calls["writes"], "log_bytes": log_bytes}
    return {
        "run_id": run_id, "prefix": prefix, "data_set": data_set,
        "mapping": mapping_settings(session.client("lambda"), fn),
        "sent_messages": summary["sent"], "send_calls": summary["calls"], "first_send": first.isoformat(),
        "last_stored": last.isoformat(), "run_hours": run_hours,
        "completions": len(lat), "stored": len(items), "p50_s": statistics.median(finite) if finite else None,
        "p95_s": nearest_rank(list(lat.values())), "latencies": lat,
        "calls": {k: v for k, v in calls.items() if k != "sizes"}, "batch_sizes": calls["sizes"],
        "sqs_metrics": sqs_metrics, "sqs_requests": sqs_requests, "sqs_requests_basis": basis,
        "sqs_by_action": counted["by_action"] if counted else None,
        "dlq_messages": dlq_depth(session.client("sqs"), f"{prefix}-trip-completed-dlq"),
        "log_bytes": log_bytes, "units": units, "cost": costs(units, run_hours),
    }


def idle_cost(requests: int, hours: float) -> dict:
    """The pollers' requests on a queue that gets no messages, per hour and for 30 days and a year of it."""
    per_hour = requests / hours
    usd = per_hour * PRICE["sqs_request"]
    return {"requests": requests, "hours": hours, "per_hour": per_hour, "month_usd": usd * 720,
            "month_cad": usd * 720 * CAD, "year_usd": usd * 8760, "year_cad": usd * 8760 * CAD,
            "lease_pct": 100 * usd * 720 / LEASE_USD}


def split(run: dict, idle: dict) -> dict:
    """A run's SQS requests per hour, less the idle floor per hour: what the traffic itself adds."""
    load = run["sqs_requests"] / run["run_hours"]
    traffic = load - idle["per_hour"]
    return {"load_per_hour": load, "traffic_per_hour": traffic,
            "traffic_month_usd": traffic * PRICE["sqs_request"] * 720,
            "idle_share": idle["per_hour"] / load if load else 0.0}


def measure_idle(session, prefix: str, trail_prefix: str, start: datetime, end: datetime) -> dict:
    queue, fn = f"{prefix}-trip-completed", f"{prefix}-trip-completed-consumer"
    counted = queue_requests(trail_counts(session, trail_prefix, start, end) or {}, queue)
    cw = session.client("cloudwatch")
    empty = int(metric_sum(cw, "AWS/SQS", "NumberOfEmptyReceives", {"QueueName": queue}, start, end))
    sent = int(metric_sum(cw, "AWS/SQS", "NumberOfMessagesSent", {"QueueName": queue}, start, end))
    calls = int(metric_sum(cw, "AWS/Lambda", "Invocations", {"FunctionName": fn}, start, end))
    hours = (end - start).total_seconds() / 3600
    return {"prefix": prefix, "start": start.isoformat(), "end": end.isoformat(),
            "mapping": mapping_settings(session.client("lambda"), fn), "by_action": counted["by_action"],
            "empty_receives": empty, "messages_sent": sent, "invocations": calls,
            **idle_cost(counted["total"], hours)}


NOT_MEASURED = "not measured"


def idle_rows(runs: list[dict], idles: list[dict | None]) -> list[tuple[str, list[str]]]:
    """The idle rows, one value per run; a run without an idle measurement (None) reads "not measured"."""
    def each(fmt, items):
        return [fmt(i) if i is not None else NOT_MEASURED for i in items]

    idles = list(idles) + [None] * (len(runs) - len(idles))
    rows = [("idle: SQS requests counted (no messages sent)", each(lambda i: f"{i['requests']:,} in {i['hours'] * 60:.0f} min", idles)),
            ("idle: ReceiveMessage / empty (CloudWatch)", each(lambda i: f"{i['by_action'].get('ReceiveMessage', 0):,} / "
                                                                         f"{i['empty_receives']:,}", idles)),
            ("idle: requests per hour", each(lambda i: f"{i['per_hour']:,.0f}", idles)),
            ("idle: 30 days, USD", each(lambda i: f"{i['month_usd']:.2f} ({i['month_cad']:.2f} CAD)", idles)),
            ("idle:   % of the 30 USD lease", each(lambda i: f"{i['lease_pct']:.1f}%", idles)),
            ("idle: a year, USD", each(lambda i: f"{i['year_usd']:.2f} ({i['year_cad']:.2f} CAD)", idles))]
    parts = [split(r, i) if i is not None else None for r, i in zip(runs, idles)]
    rows += [("load: SQS requests per hour", each(lambda x: f"{x['load_per_hour']:,.0f}", parts)),
             ("traffic part: load minus idle, per hour", each(lambda x: f"{x['traffic_per_hour']:,.0f}", parts)),
             ("traffic part of SQS, 30 days, USD", each(lambda x: f"{x['traffic_month_usd']:.2f} "
                                                                  f"({x['traffic_month_usd'] * CAD:.2f} CAD)", parts)),
             ("idle share of the load's SQS requests", each(lambda x: f"{100 * x['idle_share']:.0f}%", parts))]
    return rows


def table(runs: list[dict], idles: list[dict | None] | None = None) -> str:
    """The comparison a slide quotes: one column per run, rows of measured units, then the computed costs."""
    def sec(x):
        return f"{x:.2f}" if x < 10 else f"{x:.1f}"

    def col(r):
        m, c, k = r["mapping"], r["calls"], r["cost"]
        req = r["sqs_by_action"] or {}
        return [
            f"{m['window_s']} s, batch {m['batch_size']}",
            f"{r['sent_messages']:,}", f"{r['run_hours']:.2f}", f"{r['sent_messages'] * 720 / r['run_hours']:,.0f}",
            f"{c['calls']:,}", f"{c['mean_batch']:.2f} / {c['median_batch']:g}",
            f"{c['mean_ms']:.0f} / {c['median_ms']:.0f}", f"{c['billed_ms'] / 1000:,.1f}",
            f"{sec(r['p50_s'])} / {sec(r['p95_s'])}", f"{r['stored']} of {r['completions']}", f"{r['dlq_messages']}",
            f"{req.get('ReceiveMessage', 0):,}", f"{r['sqs_metrics']['NumberOfEmptyReceives']:,}",
            f"{req.get('ReceiveMessage', 0) - r['sqs_metrics']['NumberOfEmptyReceives']:,}",
            f"{req.get('DeleteMessageBatch', 0) + req.get('DeleteMessage', 0):,}",
            f"{r['sqs_requests']:,}", f"{r['sqs_requests'] / r['sent_messages']:.2f}", f"{r['log_bytes'] / 1024:,.0f}",
            f"{k['parts']['sqs']:.5f} / {k['parts']['lambda']:.5f}", f"{k['run_usd']:.5f}",
            f"{k['month_usd']:.2f} ({k['month_cad']:.2f} CAD)", f"{k['lease_pct']:.1f}%",
            f"{k['year_usd']:.2f} ({k['year_cad']:.2f} CAD)", f"{k['year_lease_pct']:.0f}%",
        ]
    labels = ["batching window, batch size (mapping)", "messages sent (SIM01, one replay)", "run hours (first send to last stored)",
              "messages in 30 days at this rate",
              "Lambda calls", "messages per call, mean / median", "call duration ms, mean / median",
              "billed Lambda seconds", "latency send to stored s, median / p95", "completions stored",
              "messages in the dead-letter queue", "ReceiveMessage calls (CloudTrail)", "  of them empty (CloudWatch)",
              "  of them with messages",
              "DeleteMessage(Batch) calls (CloudTrail)", "SQS requests billed, all actions", "  per message sent", "log KB ingested",
              "run USD: SQS / Lambda", "run USD, all parts", "30 days of such traffic, USD",
              "  % of the 30 USD lease", "a year, USD", "  % of the lease"]
    cols = [col(r) for r in runs]
    if idles:
        for label, values in idle_rows(runs, idles):
            labels.append(label)
            for c, v in zip(cols, values):
                c.append(v)
    width = max(len(x) for x in labels) + 2
    widths = [max(len(c[i]) for i in range(len(labels))) + 2 for c in cols]
    out = []
    for i, label in enumerate(labels):
        out.append(label.ljust(width) + "".join(c[i].rjust(w) for c, w in zip(cols, widths)))
    basis = {r["sqs_requests_basis"] for r in runs}
    out += ["", "Measured in AWS (ca-central-1) with ext-queue-ingest/run.py --target aws --speed 1, SIM01 replayed to "
            "SQS at the hour's pace;", "the runs: " + ", ".join(r["run_id"] for r in runs) + ".",
            "SQS requests: " + "; ".join(sorted(basis)) + ". DynamoDB writes and the trips service's sends are the "
            "same in both and are in the totals.",
            "Cost = the run's measured units at the course price sheet (ca-central-1, 2026-11-18; SQS 0.40 per "
            "million requests, Lambda arm64 128 MB),", "times 720 h (8,760 h) / run hours: traffic like this run's, "
            "back to back. CAD at 1.45. Free tiers left out."]
    if idles:
        out += ["Idle: the same mappings, switched on, with no message sent for "
                + " and ".join(sorted({f"{i['hours'] * 60:.0f} min from {i['start'][:16]}Z" for i in idles if i}))
                + "; the SQS requests counted by CloudTrail over that window.",
                "Lambda's pollers long-poll the queue; when traffic is low Lambda can go down to 2 concurrent "
                "invokes to cut SQS calls, but not with MaximumConcurrency set",
                "(this recipe sets 2): https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html"]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    import boto3

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id")
    p.add_argument("--data-set", default="rush-hour")
    p.add_argument("--requests", help="request_count.py's JSON counts (count --json FILE) instead of the trail's")
    p.add_argument("--trail-prefix", default=os.environ.get("TRAIL_PREFIX"),
                   help="the PREFIX request_count.py up ran with (default: PREFIX); counted over the run's window")
    p.add_argument("--table", nargs="+", metavar="RUN_ID", help="print the measured runs side by side")
    p.add_argument("--idle", nargs="*", metavar="PREFIX",
                   help="with --start/--end: count the idle requests of $PREFIX (no messages sent); with --table: "
                        "the idle measurements to add, one per run, in the same order (- for a run without one)")
    p.add_argument("--start", help="--idle: the window's start (ISO 8601, UTC)")
    p.add_argument("--end", help="--idle: the window's end")
    args = p.parse_args(argv)
    if args.table:
        runs = [json.loads((ROOT / "evidence" / r / "measured.json").read_text()) for r in args.table]
        idles = [None if x == "-" else json.loads((ROOT / "evidence" / f"idle-{x}.json").read_text())
                 for x in (args.idle or [])]
        print(table(runs, idles or None), end="")
        return 0
    if args.idle is not None:
        import boto3

        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
        start, end = parse(args.start), parse(args.end)
        for prefix in args.idle:
            got = measure_idle(session, prefix, args.trail_prefix or os.environ.get("PREFIX", prefix), start, end)
            (ROOT / "evidence").mkdir(exist_ok=True)
            (ROOT / "evidence" / f"idle-{prefix}.json").write_text(json.dumps(got, indent=1))
            print(f"{prefix} idle {got['hours'] * 60:.0f} min: {got['requests']} SQS requests ({got['by_action']}), "
                  f"{got['empty_receives']} empty receives, {got['messages_sent']} sent, {got['invocations']} calls; "
                  f"{got['per_hour']:.0f} an hour: 30 days {got['month_usd']:.2f} USD ({got['month_cad']:.2f} CAD), "
                  f"{got['lease_pct']:.1f}% of the lease; a year {got['year_usd']:.2f} USD ({got['year_cad']:.2f} CAD)")
        return 0
    if not args.run_id:
        p.error("--run-id or --table is needed")
    prefix = os.environ.get("PREFIX", "recipe")
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    requests = json.loads(Path(args.requests).read_text()) if args.requests else None
    got = measure(session, prefix, args.run_id, args.data_set, requests, args.trail_prefix or prefix)
    (ROOT / "evidence" / args.run_id / "measured.json").write_text(json.dumps(got, indent=1))
    window = got["mapping"]["window_s"]
    ok = report(got["latencies"], window)
    c, k = got["calls"], got["cost"]
    print(f"batches: {got['sqs_metrics']['NumberOfMessagesReceived']} messages received in {c['calls']} calls, "
          f"mean batch {c['mean_batch']:.2f}, median {c['median_batch']:g}; calls {c['mean_ms']:.0f} ms mean, "
          f"{c['billed_ms'] / 1000:.1f} s billed; {got['sqs_metrics']['NumberOfEmptyReceives']} empty receives; "
          f"{got['send_calls']} send calls; {got['sqs_requests']} SQS requests ({got['sqs_requests_basis']})")
    print(f"dead-letter queue: {got['dlq_messages']} messages")
    print(f"30 days at this rate: {k['month_usd']:.2f} USD ({k['month_cad']:.2f} CAD), {k['lease_pct']:.1f}% of the "
          f"30 USD lease; a year {k['year_usd']:.2f} USD ({k['year_cad']:.2f} CAD), {k['year_lease_pct']:.0f}% "
          f"(the run's measured units x 720 / {got['run_hours']:.2f} h)")
    return 0 if ok and got["dlq_messages"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
