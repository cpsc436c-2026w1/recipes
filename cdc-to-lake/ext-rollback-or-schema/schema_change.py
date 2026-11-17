"""Add a column to the lake table without breaking the queries that already run on it.

  uv run python ext-rollback-or-schema/schema_change.py                 offline: local Iceberg table
  uv run python ext-rollback-or-schema/schema_change.py --target aws    ALTER TABLE in Athena, then the steps left

The app starts storing payment_type on trips. Iceberg tracks columns by id, not by position, so adding one
rewrites no data file: old rows read NULL for it, and queries that name their columns return what they did before.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main.config import Settings  # noqa: E402
from main.lake import LocalLake, rows_to_arrow  # noqa: E402

NEW_COLUMN = ("payment_type", "string")
OLD_QUERY = "SELECT trip_id, status, fare_quote FROM trips_current ORDER BY trip_id"  # a dashboard's query


def add_column_local(table) -> None:
    from pyiceberg.types import StringType

    with table.update_schema() as u:
        u.add_column(NEW_COLUMN[0], StringType())  # optional column: rows written before it read NULL


def alter_sql(s: Settings) -> str:
    return f"ALTER TABLE {s.database}.{s.lake_table} ADD COLUMNS ({NEW_COLUMN[0]} {NEW_COLUMN[1]})"


def run_local(s: Settings, root: Path) -> str:
    from main.run import run_local as main_run

    main_run(s, root, buffer_s=0.0)
    lake = LocalLake(root, s)
    before = lake.query(OLD_QUERY)
    files_before = lake.data_files()
    add_column_local(lake.table)
    now_us = int(dt.datetime.now(dt.UTC).timestamp() * 1e6)
    lake.table.append(rows_to_arrow([{
        "trip_id": "t-005", "op": "INSERT", "change_time": now_us, "arrival_time": now_us,
        "seq": "9".rjust(40, "0"), "status": "requested", "pickup_zone": "Kerrisdale", "fare_quote": 22.0,
        "driver_id": None, "payment_type": "card"}], lake.table.schema().as_arrow()))
    after = lake.query(OLD_QUERY)
    new = lake.query("SELECT trip_id, payment_type FROM trip_changes ORDER BY trip_id, seq")
    unchanged = [r for r in after if r["trip_id"] != "t-005"] == before
    return "\n".join([
        "[local Iceberg table]",
        f"columns now: {', '.join(f.name for f in lake.table.schema().fields)}",
        f"data files before: {files_before}; after the change and one new row: {lake.data_files()}"
        " (the old files were not rewritten)",
        f"old query, same answer for the old trips: {unchanged}",
        "payment_type per change: " + ", ".join(f"{r['trip_id']}={r['payment_type']}" for r in new),
    ])


def run_aws(s: Settings) -> str:
    import boto3

    from main import athena as athena_q
    from main.infra import account_id

    account = account_id(s.region)
    athena_q.run(boto3.client("athena", region_name=s.region), alter_sql(s), s.workgroup,
                 s.athena_output(account))
    return "\n".join([
        f"ran: {alter_sql(s)}",
        "next, so new changes fill the column:",
        f"  1. add \"{NEW_COLUMN[0]}\": \"{NEW_COLUMN[1]}\" to ITEM_FIELDS in main/handler.py",
        "  2. uv run python -m main.infra --update-code     (deploys the changed function)",
        "  3. the view trips_current lists its columns; recreate it from main/sql/current_view.sql to show the new one",
    ])


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
