"""Lambda function <PREFIX>-duckdb-query: the experiment's three queries in DuckDB, on a hive-by-day table in S3.

Event: {"query": "one-day" | "one-week" | "whole-table", "folder": "s3://<bucket>/layout/trips_hive_day_<k>/"}.
A fresh in-memory DuckDB each call, its home and extension folders in /tmp (the only folder a function can write);
httpfs loaded from the package (no download at run time, no aws extension); an S3 secret from the function's own
credentials in its environment. Returns the rows (zone, trips) and the S3 requests the query sent, from DuckDB's HTTP
log: s3_gets (GET of an object) and s3_lists (GET of the bucket, a listing, billed at the LIST rate). The JSON
profile of DuckDB 2.0.1 names no request count (checked 2026-11-23), so it is not used.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
FILTERS = {"one-day": "WHERE dt = DATE '2024-03-14'",
           "one-week": "WHERE dt BETWEEN DATE '2024-03-11' AND DATE '2024-03-17'", "whole-table": ""}


def connect(local: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:", config={"home_directory": "/tmp", "extension_directory": "/tmp/duckdb_ext"})
    if not local:
        con.execute(f"LOAD '{HERE / 'httpfs.duckdb_extension'}'")
        q = lambda v: "'" + v.replace("'", "''") + "'"  # noqa: E731  (CREATE SECRET takes literals, not parameters)
        con.execute(f"CREATE SECRET s3 (TYPE s3, KEY_ID {q(os.environ['AWS_ACCESS_KEY_ID'])}, "
                    f"SECRET {q(os.environ['AWS_SECRET_ACCESS_KEY'])}, "
                    f"SESSION_TOKEN {q(os.environ.get('AWS_SESSION_TOKEN', ''))}, REGION 'ca-central-1')")
    return con


def sql_for(query: str, folder: str) -> str:
    # dt=*/* and not *.parquet: Athena's CTAS names its data files without an extension
    return (f"SELECT pickup_zone_id, count(*) AS trips FROM read_parquet('{folder.rstrip('/')}/dt=*/*', "
            f"hive_partitioning = true) {FILTERS[query]} GROUP BY 1")


def requests(log_rows) -> dict:
    """(type, url) rows of duckdb_logs_parsed('HTTP') as S3 request counts: a GET whose path is the bucket's root is
    a listing (ListObjectsV2, the glob), any other GET reads an object."""
    from urllib.parse import urlparse

    gets = [urlparse(url).path for kind, url in log_rows if kind == "GET"]
    lists = sum(1 for p in gets if p in ("", "/"))
    return {"s3_gets": len(gets) - lists, "s3_lists": lists}


def run(query: str, folder: str, con=None) -> dict:
    con = con or connect()
    con.execute("CALL enable_logging('HTTP')")
    rows = con.execute(sql_for(query, folder)).fetchall()
    sent = requests(con.execute("SELECT request.type, request.url FROM duckdb_logs_parsed('HTTP')").fetchall())
    return {"rows": sorted([int(z), int(n)] for z, n in rows), **sent}


def handler(event, context=None):
    return run(event["query"], event["folder"])
