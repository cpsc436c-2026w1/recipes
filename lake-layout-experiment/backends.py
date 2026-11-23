"""Where the experiment's tables live and its queries run.

AthenaBackend: the live run. Tables in the Glue database <prefix>_layout over s3://<PREFIX>-lake-<account>/layout/,
written by Athena CTAS in <PREFIX>-ctas-wg; measured queries in <PREFIX>-wg with result reuse off; the DuckDB engine
in the Lambda function <PREFIX>-duckdb-query.

LocalBackend: the offline stand-in. The same four layouts as local folders written by DuckDB: Hive tables as Parquet
folders (dt=<day>/ for hive-by-day), Iceberg tables as a folder of data files plus a metadata.json that records the
partition spec and one snapshot per write (a labelled stand-in, not Iceberg: real Iceberg is checked live). Data
scanned offline is the bytes of the files a query reads after pruning by folder; times are this machine's.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import timedelta
from pathlib import Path

import duckdb

from layout import (BY_DAY, COLUMNS, DAY, LAYOUTS, MB, QUERIES, REUSE_OFF, WEEK, TableBytes, build_statements,
                    ds01_ctas, query_sql, rotation_days, table)

DS01_ROWS = 6_045_943


class LocalBackend:
    name = "local"

    def __init__(self, root: Path, db: str = "layout"):
        self.root = Path(root)
        self.db = db
        self.ds01 = self.root / "ds01.parquet"
        self.calls = 0

    # --- data ---
    def extract_ds01(self, source: Path | None = None) -> int:
        """Offline DS01 is the given Parquet file (SIM01 in the scenarios) with the layout's columns."""
        self.root.mkdir(parents=True, exist_ok=True)
        duckdb.execute(f"COPY (SELECT * FROM read_parquet('{source}')) TO '{self.ds01}' (FORMAT parquet)")
        return duckdb.execute(f"SELECT count(*) FROM read_parquet('{self.ds01}')").fetchone()[0]

    def _folder(self, layout: str, copies: int) -> Path:
        return self.root / "layout" / table(layout, copies)

    def build_table(self, layout: str, copies: int, dated: bool = False) -> list[str]:
        folder = self._folder(layout, copies)
        shift = "31 * (c.copy_no - 1)" if dated else rotation_days("dayofmonth(dt)")
        select = (f"SELECT pickup_time + to_days(CAST({shift} AS INTEGER)) AS pickup_time, pickup_zone_id, "
                  f"dropoff_zone_id, trip_miles, trip_time_s, fare, driver_pay, c.copy_no AS copy_no, "
                  f"CAST(dt + to_days(CAST({shift} AS INTEGER)) AS DATE) AS dt "
                  f"FROM (SELECT unnest(range(1, {copies} + 1)) AS copy_no) c CROSS JOIN read_parquet('{self.ds01}') "
                  f"ORDER BY c.copy_no")  # one copy after the other, as the Athena statements write them (layout.py)
        kinds = [k for k, *_ in build_statements(self.db, layout, copies, str(folder), dated)]
        self._write(layout, folder, select)
        return kinds

    def _write(self, layout: str, folder: Path, select: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        part = f"part-{uuid.uuid4().hex[:8]}.parquet"
        if layout == "hive-flat":
            duckdb.execute(f"COPY ({select}) TO '{folder / part}' (FORMAT parquet, COMPRESSION zstd)")
        elif layout == "hive-by-day":
            duckdb.execute(f"COPY ({select}) TO '{folder}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (dt), "
                           f"OVERWRITE_OR_IGNORE true, "
                           f"FILENAME_PATTERN 'part-{uuid.uuid4().hex[:8]}-{{i}}')")
        else:
            data = folder / "data"
            data.mkdir(exist_ok=True)
            meta_path = folder / "metadata.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {
                "format": "iceberg stand-in (offline)", "partition_spec":
                    "day(pickup_time)" if layout == "iceberg-by-day" else None, "snapshots": []}
            if layout == "iceberg-by-day":
                duckdb.execute(f"COPY (SELECT *, CAST(pickup_time AS DATE) AS pickup_time_day FROM ({select})) "
                               f"TO '{data}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (pickup_time_day), "
                               f"OVERWRITE_OR_IGNORE true, FILENAME_PATTERN 'part-{uuid.uuid4().hex[:8]}-{{i}}')")
            else:
                duckdb.execute(f"COPY ({select}) TO '{data / part}' (FORMAT parquet, COMPRESSION zstd)")
            meta["snapshots"].append({"snapshot": len(meta["snapshots"]) + 1,
                                      "files": sorted(str(p.relative_to(folder)) for p in data.rglob("*.parquet"))})
            meta_path.write_text(json.dumps(meta, indent=1))

    def _files(self, layout: str, copies: int, query: str = "whole-table") -> list[Path]:
        folder = self._folder(layout, copies)
        files = sorted(folder.rglob("*.parquet"))
        if layout not in BY_DAY or query == "whole-table":
            return files
        a, b = (DAY, DAY) if query == "one-day" else WEEK
        days = {str(a + timedelta(days=i)) for i in range((b - a).days + 1)}
        key = "dt=" if layout == "hive-by-day" else "pickup_time_day="
        return [f for f in files if any(p.startswith(key) and p.split("=", 1)[1] in days for p in f.parts)]

    def _read(self, layout: str, copies: int, query: str) -> str:
        files = self._files(layout, copies, query)
        if not files:
            return "(SELECT NULL::INTEGER AS pickup_zone_id, NULL::DATE AS dt, NULL::TIMESTAMP AS pickup_time WHERE false)"
        cols = ", ".join(COLUMNS)
        hive = "true" if layout == "hive-by-day" else "false"
        listed = ", ".join(f"'{f}'" for f in files)
        return f"(SELECT {cols} FROM read_parquet([{listed}], hive_partitioning = {hive}, union_by_name = true))"

    # --- what the tests and the build read ---
    def count(self, layout: str, copies: int) -> int:
        return duckdb.execute(f"SELECT count(*) FROM {self._read(layout, copies, 'whole-table')}").fetchone()[0]

    def columns(self, layout: str, copies: int) -> list[str]:
        cur = duckdb.execute(f"SELECT * FROM {self._read(layout, copies, 'whole-table')} LIMIT 0")
        return [d[0] for d in cur.description]

    def folders(self, layout: str, copies: int) -> list[str]:
        return sorted(p.name for p in self._folder(layout, copies).iterdir() if p.is_dir() and "=" in p.name)

    def partitions(self, layout: str, copies: int) -> list[str]:
        folder = self._folder(layout, copies)
        if layout == "hive-by-day":
            return [f.split("=", 1)[1] for f in self.folders(layout, copies)]
        if layout == "iceberg-by-day":
            return sorted(p.name.split("=", 1)[1] for p in (folder / "data").iterdir() if p.is_dir())
        return []

    def snapshots(self, layout: str, copies: int) -> int:
        meta = self._folder(layout, copies) / "metadata.json"
        return len(json.loads(meta.read_text())["snapshots"]) if meta.exists() else 0

    def table_bytes(self, layout: str, copies: int) -> TableBytes:
        size = lambda files: sum(f.stat().st_size for f in files)  # noqa: E731
        return TableBytes(size(self._files(layout, copies)) / copies,
                          size(self._files(layout, copies, "one-day")) / copies,
                          size(self._files(layout, copies, "one-week")) / copies)

    def run_query(self, layout: str, copies: int, query: str) -> dict:
        self.calls += 1
        files = self._files(layout, copies, query)
        sql = query_sql("t", layout, copies, query).replace(f"t.{table(layout, copies)}", self._read(layout, copies, query))
        t0 = time.perf_counter()
        rows = duckdb.execute(sql).fetchall()
        ms = (time.perf_counter() - t0) * 1000
        return {"query_execution_id": f"local-{self.calls:05d}", "state": "SUCCEEDED", "reason": "", "reused": False,
                "data_scanned_bytes": sum(f.stat().st_size for f in files), "engine_ms": ms, "queue_ms": 0.0,
                "planning_ms": 0.0, "to_last_row_ms": ms, "rows": sorted(rows)}

    def probe_mb(self, copies: int) -> float:
        return min(self.run_query(lay, copies, "one-day")["data_scanned_bytes"] for lay in BY_DAY) / MB

    def duckdb_call(self, copies: int, query: str, cold: bool) -> dict:
        """The function's code run here on the local folder; the REPORT values are this machine's time."""
        out = self.run_query("hive-by-day", copies, query)
        billed = max(1, round(out["engine_ms"]))
        return {"request_id": f"local-rq-{self.calls:05d}", "duration_ms": out["engine_ms"],
                "billed_duration_ms": billed, "init_duration_ms": 1.0 if cold else None, "memory_mb": 2048,
                "s3_gets": len(self._files("hive-by-day", copies, query)), "rows": out["rows"],
                "to_last_row_ms": out["to_last_row_ms"]}

    def late_insert(self, row_select: str, copies: int = 1) -> None:
        for layout in LAYOUTS:
            self._write(layout, self._folder(layout, copies), row_select)

    def one_day_count(self, layout: str, copies: int, zone: int) -> int:
        rows = dict(self.run_query(layout, copies, "one-day")["rows"])
        return rows.get(zone, 0)

    def clean(self, yes: bool = False) -> list[str]:
        import shutil

        what = [str(self.root / "layout"), str(self.ds01)]
        if yes:
            shutil.rmtree(self.root / "layout", ignore_errors=True)
            self.ds01.unlink(missing_ok=True)
        return what


class AthenaBackend:
    """The live run (run by the course 2026-11-23: output/live.txt). Each call is one Athena statement or one Lambda invocation."""

    name = "athena"

    def __init__(self, names, sess=None, poll_s: float = 0.5):
        import boto3

        self.n = names
        self.sess = sess or boto3.Session(region_name="ca-central-1")
        self.athena = self.sess.client("athena")
        self.s3 = self.sess.client("s3")
        self.poll_s = poll_s

    @property
    def location(self) -> str:
        return f"s3://{self.n.bucket}/layout/"

    def statement(self, sql: str, workgroup: str | None = None, reuse_off: bool = False, timeout_s: float = 1800,
                  context: bool = True, poll_s: float | None = None):
        """Run one statement and wait; returns (GetQueryExecution's QueryExecution, caller ms to the last row).
        context=False for statements that run before the database exists or name their catalog."""
        kw = {"ResultReuseConfiguration": REUSE_OFF} if reuse_off else {}
        if context:
            kw["QueryExecutionContext"] = {"Database": self.n.database}
        t0 = time.perf_counter()
        qid = self.athena.start_query_execution(QueryString=sql, WorkGroup=workgroup or self.n.ctas_wg, **kw)[
            "QueryExecutionId"]
        end = time.time() + timeout_s
        while True:
            q = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() > end:
                self.athena.stop_query_execution(QueryExecutionId=qid)
                raise TimeoutError(f"{qid} still running after {timeout_s} s: {sql[:80]}")
            time.sleep(self.poll_s if poll_s is None else poll_s)
        rows = []
        if q["Status"]["State"] == "SUCCEEDED" and q.get("StatementType") != "DDL":
            token = None
            while True:
                kw2 = {"NextToken": token} if token else {}
                page = self.athena.get_query_results(QueryExecutionId=qid, MaxResults=1000, **kw2)
                rows += [[d.get("VarCharValue") for d in r["Data"]] for r in page["ResultSet"]["Rows"]]
                token = page.get("NextToken")
                if not token:
                    break
        ms = (time.perf_counter() - t0) * 1000
        q["_rows"] = rows
        return q, ms

    def must(self, sql: str, workgroup: str | None = None, context: bool = True) -> dict:
        q, _ = self.statement(sql, workgroup, context=context)
        if q["Status"]["State"] != "SUCCEEDED":
            raise SystemExit(f"{sql.splitlines()[0][:100]}: {q['Status'].get('StateChangeReason', '')}")
        return q

    def ensure_course_catalog(self, catalog_id: str) -> str:
        """The course catalog as the data source `course` (as tutorial B3T1 registers it); recorded borrowed."""
        from tags import record

        names = [c["CatalogName"] for c in self.athena.list_data_catalogs()["DataCatalogsSummary"]]
        if "course" not in names:
            self.athena.create_data_catalog(Name="course", Type="GLUE", Parameters={"catalog-id": catalog_id})
        record("athena-data-source", "course", "borrowed", script="experiment.py build",
               clean="never deleted: the course's")
        return "registered" if "course" not in names else "found"

    def check_source_columns(self) -> None:
        """The column names of course_rides.rides are read from the TLC file and tutorial B3T1, not from the
        catalog: SHOW COLUMNS settles them before the build reads the table."""
        q = self.must("SHOW COLUMNS IN course.course_rides.rides", context=False)
        cols = {r[0].strip().lower() for r in q["_rows"] if r and r[0]}
        need = {"pickup_datetime", "pulocationid", "dolocationid", "trip_miles", "trip_time", "base_passenger_fare",
                "driver_pay", "hvfhs_license_num"}
        if need - cols:
            raise SystemExit(f"course_rides.rides lacks {sorted(need - cols)}; change the SELECT in backends.py")

    def extract_ds01(self, source=None) -> int:
        self.must(f"CREATE DATABASE IF NOT EXISTS {self.n.database}", context=False)
        if not self.exists("ds01"):  # a build that stopped part-way is run again from where it stopped
            self.must(ds01_ctas(self.n.database, self.location))
        return int(self.must(f"SELECT count(*) FROM {self.n.database}.ds01")["_rows"][1][0])

    def exists(self, name: str) -> bool:
        """Whether the Glue table <prefix>_layout.<name> exists (a rerun of build skips the tables it has)."""
        glue = self.sess.client("glue")
        try:
            glue.get_table(DatabaseName=self.n.database, Name=name)
            return True
        except glue.exceptions.EntityNotFoundException:
            return False

    def empty_location(self, prefix: str) -> int:
        """Delete what a stopped CTAS left under a table's prefix (a CTAS needs an empty location); the count."""
        keys = [o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.n.bucket,
                                                                                       Prefix=prefix)
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(Bucket=self.n.bucket,
                                   Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
        return len(keys)

    def build_table(self, layout: str, copies: int, dated: bool = False) -> list[str]:
        loc = f"{self.location}{table(layout, copies)}/"
        statements = build_statements(self.n.database, layout, copies, loc, dated)
        if self.exists(table(layout, copies)):
            # Built by an earlier build that stopped later on: its rows are checked by count() after the growth.
            return [f"{k} (kept from an earlier build)" for k, *_ in statements[:1]]
        self.empty_location(f"layout/{table(layout, copies)}/")  # a CTAS stopped part-way leaves files and no table
        kinds = []
        for kind, sql, _a, _b in statements:
            self.must(sql)
            kinds.append(kind)
        return kinds

    def count(self, layout: str, copies: int) -> int:
        return int(self.must(f"SELECT count(*) FROM {self.n.database}.{table(layout, copies)}")["_rows"][1][0])

    def table_bytes(self, layout: str, copies: int) -> TableBytes:
        prefix = f"layout/{table(layout, copies)}/"
        sizes: dict[str, int] = {}
        total = 0
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.n.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                total += o["Size"]
                for part in o["Key"].split("/"):
                    if part.startswith(("dt=", "pickup_time_day=")):
                        sizes[part.split("=", 1)[1]] = sizes.get(part.split("=", 1)[1], 0) + o["Size"]
        days = sorted(sizes)
        day = max(sizes.values()) if sizes else total
        week = max((sum(sizes[d] for d in days[i:i + 7]) for i in range(len(days))), default=total)
        return TableBytes(total / copies, day / copies, week / copies)

    def run_query(self, layout: str, copies: int, query: str) -> dict:
        # Measured runs poll every 0.1 s: the poll is the resolution of the time to the last row (Athena's own
        # statistics are kept beside it).
        q, ms = self.statement(query_sql(self.n.database, layout, copies, query), self.n.wg, reuse_off=True,
                               poll_s=0.1)
        st = q.get("Statistics") or {}
        rows = [(int(r[0]), int(r[1])) for r in q["_rows"][1:] if r[0] is not None]
        return {"query_execution_id": q["QueryExecutionId"], "state": q["Status"]["State"],
                "reason": q["Status"].get("StateChangeReason", ""),
                "reused": bool((st.get("ResultReuseInformation") or {}).get("ReusedPreviousResult")),
                "data_scanned_bytes": int(st.get("DataScannedInBytes") or 0),
                "engine_ms": st.get("EngineExecutionTimeInMillis"), "queue_ms": st.get("QueryQueueTimeInMillis"),
                "planning_ms": st.get("QueryPlanningTimeInMillis"), "to_last_row_ms": ms, "rows": sorted(rows)}

    def probe_mb(self, copies: int) -> float:
        return min(self.run_query(lay, copies, "one-day")["data_scanned_bytes"] for lay in BY_DAY) / MB

    def duckdb_call(self, copies: int, query: str, cold: bool = False) -> dict:
        from botocore.config import Config

        # One attempt and a read timeout above the function's 300 s: a retried call would run the query twice.
        lam = self.sess.client("lambda", config=Config(read_timeout=310, connect_timeout=10,
                                                       retries={"total_max_attempts": 1}))
        payload = {"query": query, "folder": f"{self.location}{table('hive-by-day', copies)}/"}
        t0 = time.perf_counter()
        r = lam.invoke(FunctionName=self.n.function, Payload=json.dumps(payload).encode(), LogType="Tail")
        body = json.loads(r["Payload"].read() or b"{}")
        ms = (time.perf_counter() - t0) * 1000
        import base64

        from report_lines import parse_report

        rep = parse_report(base64.b64decode(r.get("LogResult", "")).decode(errors="replace")) or {}
        return {"request_id": rep.get("request_id") or r["ResponseMetadata"]["RequestId"],
                "duration_ms": rep.get("duration_ms"), "billed_duration_ms": rep.get("billed_duration_ms"),
                "init_duration_ms": rep.get("init_duration_ms"), "memory_mb": rep.get("memory_mb", 2048),
                "s3_gets": body.get("s3_gets"), "s3_lists": body.get("s3_lists"), "rows": sorted(tuple(x) for x in body.get("rows", [])),
                "to_last_row_ms": ms, "error": body.get("errorMessage", "") or r.get("FunctionError", "")}

    def late_insert(self, row_select: str, copies: int = 1) -> None:
        for layout in LAYOUTS:
            self.must(f"INSERT INTO {self.n.database}.{table(layout, copies)} {row_select}")

    def one_day_count(self, layout: str, copies: int, zone: int) -> int:
        return dict(self.run_query(layout, copies, "one-day")["rows"]).get(zone, 0)


assert QUERIES and DS01_ROWS
