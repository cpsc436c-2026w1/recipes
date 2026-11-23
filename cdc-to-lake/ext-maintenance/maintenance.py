"""Table maintenance: compact the small files a streaming writer leaves, expire old snapshots, on a schedule.

  uv run python ext-maintenance/maintenance.py                     offline: local Iceberg table, DuckDB
  uv run python ext-maintenance/maintenance.py --target aws        OPTIMIZE and VACUUM once in Athena, measured
  uv run python ext-maintenance/maintenance.py --target aws --schedule [--email you@example.com]
        the nightly function <PREFIX>-compact-days, its two schedules, two alarms and the alert topic
  uv run python ext-maintenance/maintenance.py --target aws --register-partitions
        trip_events_parquet: projection off, every day folder and the next 7 days registered
  uv run python ext-maintenance/maintenance.py --target aws --measure [--all-days]
        the 30-day count before and after compacting trip_events_parquet, after a spend estimate

Firehose commits once per buffer interval, so a 60 s buffer can add up to 1,440 commits a day, each with its own
small data files and its own snapshot. Every query then opens many files, and the metadata keeps growing.
Compaction rewrites them into fewer, larger files; snapshot expiry drops old snapshots (and with them the
time travel to those points), so VACUUM can delete the data files no snapshot uses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent),
                str(Path(__file__).resolve().parent.parent / "scripts")] if p not in sys.path]

from main.config import Settings  # noqa: E402
from main.lake import LocalLake  # noqa: E402

BENCH = "SELECT pickup_zone, count(*) AS changes, round(avg(fare_quote), 2) AS avg_fare FROM {t} GROUP BY 1"
KEEP_DAYS = 7  # ours: snapshot age kept for time travel and rollback; vacuum_max_snapshot_age_seconds in the DDL


def optimize_sql(s: Settings) -> str:
    return f"OPTIMIZE {s.database}.{s.lake_table} REWRITE DATA USING BIN_PACK"


def vacuum_sql(s: Settings) -> str:
    # VACUUM expires snapshots older than vacuum_max_snapshot_age_seconds (but keeps
    # vacuum_min_snapshots_to_keep) and removes files no snapshot uses (Athena docs, VACUUM).
    return f"VACUUM {s.database}.{s.lake_table}"


def schedule_definitions(prefix: str, function_arn: str, role_arn: str) -> list[dict]:
    """Two schedules in the tagged group <PREFIX>-maintenance-compaction, both calling the function: at 03:00 the
    rule per day (compact), at 04:00 VACUUM of each Iceberg table; Vancouver time, so they fall outside class hours;
    no retries (the next night is the retry)."""
    def target(mode: str) -> dict:
        return {"Arn": function_arn, "RoleArn": role_arn, "Input": json.dumps({"mode": mode}),
                "RetryPolicy": {"MaximumRetryAttempts": 0}}

    common = {"GroupName": f"{prefix}-maintenance-compaction", "ScheduleExpressionTimezone": "America/Vancouver",
              "FlexibleTimeWindow": {"Mode": "OFF"}, "State": "ENABLED"}
    return [
        {"Name": f"{prefix}-compact-schedule", "ScheduleExpression": "cron(0 3 * * ? *)", "Target": target("compact"),
         **common},
        {"Name": f"{prefix}-lake-vacuum", "ScheduleExpression": "cron(0 4 * * ? *)", "Target": target("vacuum"),
         **common},
    ]


# --- offline ------------------------------------------------------------------------------------------------

def stream_like_commits(lake: LocalLake, commits: int, rows_each: int, start: dt.datetime) -> None:
    """Many small commits, as Firehose makes them: one per buffer interval."""
    zones = ["Kitsilano", "Downtown", "Mount Pleasant", "Fairview", "Kerrisdale"]
    n = 0
    for c in range(commits):
        at = start + dt.timedelta(minutes=c)
        rows = []
        for _ in range(rows_each):
            us = int(at.timestamp() * 1e6) + n
            rows.append({"trip_id": f"t-{n % 400:04d}", "op": "MODIFY", "change_time": us, "arrival_time": us,
                         "seq": str(n).rjust(40, "0"), "status": "claimed", "pickup_zone": zones[n % 5],
                         "fare_quote": 10.0 + n % 17, "driver_id": f"d-{n % 30}"})
            n += 1
        lake.append(rows)


def measure_local(lake: LocalLake) -> dict:
    t0 = time.perf_counter()
    rows = lake.query(BENCH.format(t=lake.s.lake_table))
    return {"files": lake.data_files(), "snapshots": len(lake.table.snapshots()),
            "data_scanned": lake.data_scanned(), "run_ms": (time.perf_counter() - t0) * 1000, "result": rows}


def compact_local(lake: LocalLake) -> None:
    """Rewrite the table's rows into as few files as possible (one per day partition). Athena's OPTIMIZE ...
    BIN_PACK does this in place on AWS; PyIceberg does not have a compaction call, so an overwrite stands in."""
    lake.table.overwrite(lake.table.scan().to_arrow())


def expire_local(lake: LocalLake, older_than: dt.datetime) -> None:
    lake.table.maintenance.expire_snapshots().older_than(older_than).commit()


def run_local(s: Settings, root: Path, commits: int = 120, rows_each: int = 5) -> str:
    lake = LocalLake(root, s)
    stream_like_commits(lake, commits, rows_each, dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.UTC))
    first_snapshot = lake.table.snapshots()[0].snapshot_id
    before = measure_local(lake)
    compact_local(lake)
    expire_local(lake, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))  # demo: expire all but the current
    after = measure_local(lake)
    same = sorted(map(str, before["result"])) == sorted(map(str, after["result"]))
    try:
        lake.table.scan(snapshot_id=first_snapshot).to_arrow()
        travel = "still possible"
    except Exception:  # noqa: BLE001
        travel = "no longer possible (expired)"
    head = f"{'':<10}{'data files':>12}{'snapshots':>11}{'data scanned':>15}{'run time':>11}"
    row = lambda k, m: (f"{k:<10}{m['files']:>12}{m['snapshots']:>11}{m['data_scanned']:>13} B"  # noqa: E731
                        f"{m['run_ms']:>8.1f} ms")
    return "\n".join([
        f"[local Iceberg table: {commits} commits of {rows_each} rows, one per minute; DuckDB times]",
        head, row("before", before), row("after", after),
        f"same query answer before and after: {same}",
        f"time travel to the first snapshot: {travel}",
    ])


# --- AWS ----------------------------------------------------------------------------------------------------

def run_aws(s: Settings) -> str:
    import boto3

    from main import athena as athena_q
    from main.infra import account_id

    account = account_id(s.region)
    athena, out = boto3.client("athena", region_name=s.region), s.athena_output(account)
    bench = BENCH.format(t=s.lake_table)
    before = athena_q.run(athena, bench, s.workgroup, out, s.database)
    opt = athena_q.run(athena, optimize_sql(s), s.workgroup, out, s.database)
    vac = athena_q.run(athena, vacuum_sql(s), s.workgroup, out, s.database)
    after = athena_q.run(athena, bench, s.workgroup, out, s.database)
    return "\n".join([
        f"{'':<10}{'data scanned':>15}{'run time':>12}",
        f"{'before':<10}{before.data_scanned_bytes:>13} B{before.engine_ms:>9} ms",
        f"{'after':<10}{after.data_scanned_bytes:>13} B{after.engine_ms:>9} ms",
        f"OPTIMIZE: {opt.data_scanned_bytes} B scanned, {opt.engine_ms} ms; VACUUM: {vac.engine_ms} ms",
    ])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--schedule", action="store_true", help="deploy the nightly function, schedules and alarms (aws)")
    p.add_argument("--email", help="with --schedule: the address the alarms send to (subscribed to the topic)")
    p.add_argument("--register-partitions", action="store_true",
                   help="trip_events_parquet: projection off, partitions of every day folder and the next 7 days")
    p.add_argument("--measure", action="store_true", help="the 30-day count before and after compaction (aws)")
    p.add_argument("--all-days", action="store_true", help="with --measure: compact every marked day, not 10")
    args = p.parse_args(argv)
    if args.target == "local":
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            print(run_local(Settings(), Path(d)))
        return 0
    import nightly_ops
    if args.register_partitions:
        return nightly_ops.register_partitions_cli()
    if args.measure:
        return nightly_ops.measure_cli(all_days=args.all_days)
    if args.schedule:
        return nightly_ops.deploy_cli(email=args.email)
    print(run_aws(Settings()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
