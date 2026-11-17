"""One set of columns in three places: the Athena DDL, the local Iceberg schema, the Lambda function."""

import re

from main.config import Settings, sql
from main.handler import ITEM_FIELDS, KEY
from main.lake import iceberg_schema


def ddl_columns(text: str) -> list[tuple[str, str]]:
    body = text.split("CREATE TABLE", 1)[1].split("(", 1)[1].split(")\nPARTITIONED", 1)[0]
    return [tuple(line.split()[:2]) for line in body.strip().splitlines() if line.strip()]


def test_ddl_matches_iceberg_schema_and_handler():
    s = Settings()
    ddl = sql("create_table", database=s.database, table=s.lake_table, location="s3://b/lake/")
    cols = [(n, t.rstrip(",")) for n, t in ddl_columns(ddl)]
    assert [n for n, _ in cols] == [f.name for f in iceberg_schema().fields]
    assert dict(cols[5:]) == ITEM_FIELDS and cols[0] == (KEY, "string")
    assert "'table_type' = 'ICEBERG'" in ddl and "PARTITIONED BY (day(change_time))" in ddl


def test_names_follow_prefix_rules():
    s = Settings()
    assert s.table.startswith("test-") and s.delivery_stream.startswith("test-")
    assert re.fullmatch(r"[a-z0-9_]+", s.database)  # Glue and Athena names: no hyphens


def test_view_sql_runs_in_both_engines_form():
    v = sql("current_view", view="db.trips_current", table="db.trip_changes")
    assert "row_number() OVER (PARTITION BY trip_id ORDER BY change_time DESC, seq DESC)" in v
