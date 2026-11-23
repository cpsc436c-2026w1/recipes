"""The units a run used, one entry per billed part, each marked measured, computed or assumed with its source.

    uv run python scripts/units.py --run-id <id> [--since <ISO time>]     # evidence/<run-id>/units.json

A billed part the inventory lists and no reader covers gets a row {"not measured": "<part>"}, so a total is never
quietly short. Readers here: Lambda (REPORT lines), DynamoDB (the function's UNITS lines), Athena (each query id in
evidence/<run-id>/queries.json), Kinesis (shard-hours from the ledger's create and delete times; PUT payload units
from the replay's sent.jsonl), S3 requests (the replay's objects), Glue jobs (GetJobRun), CloudWatch Logs
(IncomingBytes). Other kinds (load balancers, ECS, EC2, EMR ...) are listed as not measured until you add a reader.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
from tags import ledger_rows  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = Path(__import__("os").environ.get("EVIDENCE") or ROOT / "evidence")
REPORT = re.compile(r"Billed Duration: (?P<billed>[\d.]+) ms\s+Memory Size: (?P<mb>\d+) MB")
GLUE_DPU = {"G.025X": 0.25, "G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8, "Standard": 1}
NOT_READ = {"load-balancer": ["Application Load Balancer hours", "Application Load Balancer LCU-hours"],
            "ecs-service": ["Fargate vCPU-hours", "Fargate GB-hours"], "ec2-instance": ["EC2 instance-hours"],
            "emr-cluster": ["EMR instance-hours"], "nat-gateway": ["NAT gateway hours"],
            "sqs-queue": ["SQS requests"], "schedule-group": ["Scheduler invocations"],
            "cloudwatch-alarm": ["CloudWatch alarm-months"]}


def part(name: str, units: float, how: str, source: str, **more) -> dict:
    return {"part": name, "units": units, "how": how, "source": source, **more}


def lambda_units(report_lines: list[str], arch: str = "arm64") -> list[dict]:
    """Requests and billed GB-seconds from the REPORT lines of a function's log group."""
    requests, gb_s = 0, 0.0
    for line in report_lines:
        m = REPORT.search(line)
        if m:
            requests += 1
            gb_s += float(m["billed"]) / 1000 * int(m["mb"]) / 1024
    src = "REPORT lines of the function's log group"
    return [part("Lambda requests", requests, "measured", src),
            part(f"Lambda GB-seconds, {arch}", round(gb_s, 6), "measured", src)]


def glue_dpu_hours(run: dict) -> float:
    """DPUSeconds when Glue gives it (Flex, auto scaling), else ExecutionTime x workers x the worker's DPU;
    at least 1 minute."""
    if run.get("DPUSeconds"):
        return run["DPUSeconds"] / 3600
    seconds = max(run.get("ExecutionTime", 0), 60)
    workers = run.get("NumberOfWorkers") or 0
    return seconds * workers * GLUE_DPU.get(run.get("WorkerType", "G.1X"), 1) / 3600


def shard_hours(created: str, deleted: str | None, shards: int = 1, now: datetime | None = None) -> int:
    """Whole shard-hours from the ledger's create and delete times (a part of an hour counted as an hour, assumed)."""
    t0 = datetime.fromisoformat(created.replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(deleted.replace("Z", "+00:00")) if deleted else (now or datetime.now(timezone.utc))
    return shards * max(1, math.ceil((t1 - t0).total_seconds() / 3600))


def read_run(sess, run_id: str, since: datetime, rows: list | None = None) -> dict:
    """Every reader for the parts of this inventory."""
    rows = rows if rows is not None else inventory.take(sess)
    ev = EVIDENCE / run_id
    parts, start_ms = [], int(since.timestamp() * 1000)
    kinds = {r.kind for r in rows}
    logs = sess.client("logs")
    for r in [r for r in rows if r.kind == "lambda-function"]:
        group = f"/aws/lambda/{r.name}"
        lines, units = [], []
        try:
            for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group, startTime=start_ms):
                for e in page["events"]:
                    (lines if e["message"].startswith("REPORT") else units).append(e["message"])
        except logs.exceptions.ResourceNotFoundException:
            pass
        parts += [{**p, "resource": r.name} for p in lambda_units(lines)]
        wu = sum(json.loads(u.split("UNITS ", 1)[1])["write_units"] for u in units if "UNITS " in u)
        if units:
            parts.append(part("DynamoDB write units", wu, "measured", f"UNITS lines of {group} (ConsumedCapacity)"))
    athena = sess.client("athena")
    queries = json.loads((ev / "queries.json").read_text()) if (ev / "queries.json").exists() else []
    if queries:
        mb = 0.0
        for q in queries:
            stats = athena.get_query_execution(QueryExecutionId=q)["QueryExecution"].get("Statistics", {})
            mb += max(stats.get("DataScannedInBytes", 0) / 2 ** 20, 10)
        parts.append(part("Athena Data scanned, MB", round(mb, 3), "measured",
                          "GetQueryExecution DataScannedInBytes, each query billed at least 10 MB",
                          queries=len(queries)))
    ledger = ledger_rows()
    for r in [r for r in rows if r.kind == "kinesis-stream"]:
        made = [x["time"] for x in ledger if x["type"] == "kinesis-stream" and x["name"] == r.name
                and x["state"] == "created"]
        gone = [x["time"] for x in ledger if x["type"] == "kinesis-stream" and x["name"] == r.name
                and x["state"] == "deleted"]
        if made:
            parts.append(part("Kinesis shard-hours", shard_hours(made[-1], gone[-1] if gone else None), "computed",
                              "the ledger's create and delete times, whole hours", resource=r.name, hourly=True,
                              count=1))
    sent = ev / "sent.jsonl"
    if sent.exists():
        n = sum(1 for _ in open(sent))
        if "kinesis-stream" in kinds:
            parts.append(part("Kinesis PUT payload units", n, "computed",
                              "sent.jsonl: one record each, every one under 25 KB"))
    glue = sess.client("glue")
    for r in [r for r in rows if r.kind == "glue-job"]:
        for run in glue.get_job_runs(JobName=r.name).get("JobRuns", []):
            parts.append(part("Glue DPU-hours", round(glue_dpu_hours(run), 4), "computed", "GetJobRun",
                              resource=r.name))
    return {"run_id": run_id, "since": since.isoformat(), "parts": parts + not_measured(kinds)}


def not_measured(kinds: set[str]) -> list[dict]:
    return [{"not measured": p} for k in sorted(kinds) for p in NOT_READ.get(k, [])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--since", help="ISO time the run started (default: 3 hours ago)")
    args = ap.parse_args(argv)
    since = (datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since
             else datetime.now(timezone.utc) - timedelta(hours=3))
    units = read_run(inventory.session(), args.run_id, since)
    out = EVIDENCE / args.run_id / "units.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(units, indent=1))
    for p in units["parts"]:
        print(f"units    {p}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
