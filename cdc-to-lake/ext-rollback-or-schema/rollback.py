"""Undo a bad load with Iceberg snapshots: find it by time travel, roll back, keep the good commits after it.

  uv run python ext-rollback-or-schema/rollback.py                  offline: local Iceberg table
  uv run python ext-rollback-or-schema/rollback.py --target aws     the Glue Data Catalog table of main/

The bad load: a backfill job writes every trip again with fare_quote in cents instead of dollars. Every commit
to an Iceberg table is a snapshot, so the table as it was before the load is still there to read and to return to.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from main.config import Settings  # noqa: E402
from main.lake import LocalLake, rows_to_arrow  # noqa: E402

CHECK = "SELECT count(*) AS changes, round(avg(fare_quote), 2) AS avg_fare FROM t"


def stats(table, snapshot_id: int | None = None) -> dict:
    """The same check at any snapshot: time travel is a scan with a snapshot id.
    Athena: SELECT ... FROM trip_changes FOR VERSION AS OF <snapshot id>."""
    con = duckdb.connect()
    con.register("t", table.scan(snapshot_id=snapshot_id).to_arrow())
    changes, avg_fare = con.execute(CHECK).fetchone()
    return {"changes": changes, "avg_fare": avg_fare}


def bad_backfill(table, now_us: int) -> list[dict]:
    """What the faulty job writes: each trip's latest row again, fare_quote multiplied by 100."""
    rows = table.scan().to_arrow().to_pylist()
    latest = {}
    for r in sorted(rows, key=lambda r: (r["change_time"], r["seq"])):
        latest[r["trip_id"]] = r
    out = []
    for r in latest.values():
        bad = {k: v for k, v in r.items()}
        bad.update(op="MODIFY", change_time=now_us, arrival_time=now_us, seq="backfill".rjust(40, "~"),
                   fare_quote=None if r["fare_quote"] is None else r["fare_quote"] * 100)
        out.append(bad)
    return out


def _to_us(v):
    return v if isinstance(v, int) else int(v.replace(tzinfo=dt.UTC).timestamp() * 1e6)


def restore(table, bad_snapshot_id: int) -> dict:
    """Roll back to the snapshot before the bad one, then append again the rows good commits added after it.

    Rolling back alone would also drop every change the stream delivered after the bad load. The rows to keep
    are those in the current snapshot that the bad snapshot did not have, matched on (trip_id, seq).
    """
    bad = table.snapshot_by_id(bad_snapshot_id)
    parent = bad.parent_snapshot_id
    in_bad = {(r["trip_id"], r["seq"]) for r in
              table.scan(snapshot_id=bad_snapshot_id, selected_fields=("trip_id", "seq")).to_arrow().to_pylist()}
    later = [r for r in table.scan().to_arrow().to_pylist() if (r["trip_id"], r["seq"]) not in in_bad]
    table.manage_snapshots().rollback_to_snapshot(parent).commit()
    if later:
        rows = [{k: (_to_us(v) if k in ("change_time", "arrival_time") and v is not None else v)
                 for k, v in r.items()} for r in later]
        table.append(rows_to_arrow(rows, table.schema().as_arrow()))
    return {"rolled_back_to": parent, "good_rows_kept": len(later)}


def demo(table, append, now_us: int, later_rows: list[dict] | None = None) -> list[str]:
    before_id = table.current_snapshot().snapshot_id
    before = stats(table)
    bad_id = append(bad_backfill(table, now_us))
    if later_rows:
        append(later_rows)  # the stream keeps delivering after the bad load
    after_bad = stats(table)
    result = restore(table, bad_id)
    restored = stats(table)
    return [
        f"before the load   snapshot {before_id}: {before['changes']} changes, average fare {before['avg_fare']}",
        f"after the load    snapshot {bad_id} and later commits: {after_bad['changes']} changes, average fare {after_bad['avg_fare']}"
        "   <- fares 100 times too high",
        f"time travel to {before_id}: {stats(table, before_id)['changes']} changes (the earlier table is still there)",
        f"rolled back to {result['rolled_back_to']}, good rows from later commits appended again: "
        f"{result['good_rows_kept']}",
        f"after restore: {restored['changes']} changes, average fare {restored['avg_fare']}",
    ]


def run_local(s: Settings, root: Path) -> str:
    from main.run import run_local as main_run

    main_run(s, root, buffer_s=0.0)  # the main scenario fills the table
    lake = LocalLake(root, s)
    now_us = int(dt.datetime.now(dt.UTC).timestamp() * 1e6)
    later = [{"trip_id": "t-004", "op": "INSERT", "change_time": now_us + 1, "arrival_time": now_us + 2,
              "seq": "9".rjust(40, "0"), "status": "requested", "pickup_zone": "Fairview", "fare_quote": 9.75,
              "driver_id": None}]
    lines = demo(lake.table, lake.append, now_us, later)
    return "[local Iceberg table]\n" + "\n".join(lines)


def catalog_properties(s: Settings, session=None) -> dict:
    """What PyIceberg needs to reach Glue and S3: the region and the credentials boto3 resolved, written out.

    PyIceberg reads the table's files in S3 through pyarrow, which looks for credentials on its own and does not
    find every kind boto3 finds (with a profile that takes them from the EC2 instance, S3 answered ACCESS_DENIED).
    Handing over boto3's credentials makes Glue and S3 use the same identity as every other call of the recipe.
    They are read here, at run time, and are not written anywhere.
    """
    import boto3

    session = session or boto3.Session(region_name=s.region)
    props = {"client.region": s.region}
    found = session.get_credentials()
    if found is not None:
        creds = found.get_frozen_credentials()
        props["client.access-key-id"] = creds.access_key
        props["client.secret-access-key"] = creds.secret_key
        if creds.token:
            props["client.session-token"] = creds.token
    return props


def run_aws(s: Settings) -> str:
    """Pause the stream consumer, find and undo the bad snapshot, resume. output/ext-rollback.txt is a run on AWS."""
    import boto3
    from pyiceberg.catalog.glue import GlueCatalog

    table = GlueCatalog("glue", **catalog_properties(s)).load_table(f"{s.database}.{s.lake_table}")
    lam = boto3.client("lambda", region_name=s.region)
    mappings = lam.list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"]
    # Pause: Firehose commits during the restore would race it. The stream keeps the records (24 hours) and the
    # mapping resumes from where it stopped, so no change is lost.
    for m in mappings:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=False)
    try:
        def append(rows):
            table.append(rows_to_arrow(rows, table.schema().as_arrow()))
            return table.current_snapshot().snapshot_id

        lines = demo(table, append, int(dt.datetime.now(dt.UTC).timestamp() * 1e6))
    finally:
        for m in mappings:
            lam.update_event_source_mapping(UUID=m["UUID"], Enabled=True)
    return "[aws: Glue Data Catalog table]\n" + "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args()
    s = Settings()
    if args.target == "aws":
        print(run_aws(s))
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            print(run_local(s, Path(d)))


if __name__ == "__main__":
    main()
