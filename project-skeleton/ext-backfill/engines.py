"""Where the backfill's statements run: Athena in the sandbox, or DuckDB over local files offline.

Both engines take the same calls from backfill.py and keep the Athena text of every statement they send (or, offline,
would send) in `sent`, with its kind and workgroup. Offline the statements are also appended to
`.local/ext-backfill/statements.jsonl`, so a test can read them after the run.
"""
from __future__ import annotations

import io
import json
import shutil
import time
from datetime import date, datetime
from pathlib import Path

import statements as sql
from statements import ATHENA, DUCKDB, Source

LOCAL = Path(".local") / "ext-backfill"
HISTORY_PREFIX = "history/trips/"
SOURCE_KEY = "history/trips.source"   # which source trips_history was copied from; beside the prefix, not in it
HISTORY_TYPES = {**{c: "varchar" for c in sql.HISTORY_COLUMNS}, **sql.TYPES, "requested_day": "date"}
SAMPLE = Path(__file__).resolve().parent / "sim" / "SIM01-march-sample.csv"   # the course table, offline


class Engine:
    def __init__(self, db: str, workgroup: str, ctas_workgroup: str):
        self.db, self.wg, self.ctas_wg = db, workgroup, ctas_workgroup
        self.sent: list[dict] = []
        self.scanned = 0

    def close(self) -> None:
        pass

    def _sent(self, kind: str, workgroup: str, text: str) -> None:
        self.sent.append({"kind": kind, "workgroup": workgroup, "sql": text})

    # what backfill.py asks of an engine
    def relation(self, src: Source, dialect: str) -> str:
        if src.kind == "course":
            return "course.course_rides.rides" if dialect == ATHENA else "source_course"
        return f"{self.db}.history_raw" if dialect == ATHENA else "history_raw"


class LocalEngine(Engine):
    """DuckDB stands in for Athena; `.local/ext-backfill/lake/history/trips/` for the bucket's prefix."""

    def __init__(self, db: str, workgroup: str, ctas_workgroup: str, root: Path = LOCAL, sample: Path | None = None,
                 run_id: str = ""):
        super().__init__(db, workgroup, ctas_workgroup)
        self.run_id = run_id
        import duckdb

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir = root / "lake" / HISTORY_PREFIX
        self.con = duckdb.connect(str(root / "athena.duckdb"))
        self.sample = Path(sample) if sample else SAMPLE
        self.location = f"s3://<bucket>/{HISTORY_PREFIX}"

    def history_source(self) -> str | None:
        path = self.root / "lake" / SOURCE_KEY
        return path.read_text().strip() if path.exists() else None

    def set_history_source(self, source: str) -> None:
        path = self.root / "lake" / SOURCE_KEY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source + "\n")

    def _sent(self, kind, workgroup, text):
        super()._sent(kind, workgroup, text)
        with open(self.root / "statements.jsonl", "a") as f:
            f.write(json.dumps({"run": self.run_id, "kind": kind, "workgroup": workgroup, "sql": text}) + "\n")

    def close(self) -> None:
        self.con.close()

    def _catalog(self) -> dict:
        path = self.root / "catalog.json"
        return json.loads(path.read_text()) if path.exists() else {"tables": []}

    def _add_table(self, name: str) -> None:
        cat = self._catalog()
        if name not in cat["tables"]:
            cat["tables"].append(name)
        (self.root / "catalog.json").write_text(json.dumps(cat))

    def table_exists(self, name: str) -> bool:
        return name in self._catalog()["tables"]

    def _history(self) -> str:
        files = list(self.history_dir.glob("requested_day=*/*.parquet"))
        if not files:
            cols = ", ".join(f"CAST(NULL AS {HISTORY_TYPES[c]}) AS {c}" for c in sql.HISTORY_COLUMNS)
            return f"(SELECT {cols} WHERE false)"
        return (f"read_parquet('{self.history_dir}/requested_day=*/*.parquet', hive_partitioning = true, "
                f"hive_types = {{'requested_day': DATE}})")

    def ensure_source(self, src: Source, files: str | None = None) -> None:
        if src.kind == "course":
            # the course catalog's table, from the sample: each column with the type the course table has
            cols = ", ".join(
                f"CAST({c} AS {t.replace('string', 'varchar')}) AS {c}" if c in self._sample_columns()
                else f"CAST(NULL AS {t.replace('string', 'varchar')}) AS {c}"
                for c, t in src.columns.items())
            self.con.execute(f"CREATE OR REPLACE TABLE source_course AS SELECT {cols} "
                             f"FROM read_csv('{self.sample}', header = true, all_varchar = true)")
            return
        self._sent("ddl", self.ctas_wg, sql.external_table(src, self.db, files or ""))
        folder = Path(files or "")
        names = list(src.columns)
        if src.format == "csv":
            types = {c: "VARCHAR" for c in names}
            rel = (f"read_csv('{folder}/*.csv', header = true, columns = {types!r}, "
                   f"auto_detect = false, quote = '\"')")
        elif src.format == "jsonl":
            rel = f"read_json('{folder}/*.jsonl', format = 'newline_delimited', columns = { {c: 'VARCHAR' for c in names}!r})"
        else:
            rel = f"read_parquet('{folder}/*.parquet')"
        self.con.execute(f"CREATE OR REPLACE VIEW history_raw AS SELECT {', '.join(names)} FROM {rel}")
        self._add_table("history_raw")

    def _sample_columns(self) -> set[str]:
        header = self.sample.read_text().splitlines()[0]
        return {c.strip().lower() for c in header.split(",")}

    def count(self, src: Source, days: list[date]) -> dict[date, int]:
        self._sent("query", self.wg, sql.source_counts(src, self.relation(src, ATHENA), days, ATHENA))
        rows = self.con.execute(sql.source_counts(src, self.relation(src, DUCKDB), days, DUCKDB)).fetchall()
        return {date.fromisoformat(d): n for d, n in rows if d}

    def history_counts(self, days: list[date]) -> dict[date, int]:
        self._sent("query", self.wg, sql.history_counts(f"{self.db}.trips_history", days))
        rows = self.con.execute(sql.history_counts(self._history(), days)).fetchall()
        return {date.fromisoformat(d): n for d, n in rows}

    def copy(self, src: Source, days: list[date], run_id: str, first: bool) -> None:
        if first:
            self._sent("ctas", self.ctas_wg, sql.ctas(src, self.relation(src, ATHENA), self.db, self.location, days,
                                                      run_id))
        else:
            self._sent("insert", self.ctas_wg, sql.insert_into(src, self.relation(src, ATHENA), self.db, days, run_id))
        select = sql.history_select(src, self.relation(src, DUCKDB), days, run_id, DUCKDB)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.con.execute(f"COPY ({select}) TO '{self.history_dir}' (FORMAT PARQUET, COMPRESSION ZSTD, "
                         f"PARTITION_BY (requested_day), APPEND true, FILENAME_PATTERN '{run_id}_{{uuid}}')")
        self._add_table("trips_history")

    def day_files(self, day: date) -> list[str]:
        return sorted(p.name for p in (self.history_dir / f"requested_day={day}").glob("*.parquet"))

    def delete_day(self, day: date) -> list[str]:
        folder = self.history_dir / f"requested_day={day}"
        gone = self.day_files(day)
        shutil.rmtree(folder, ignore_errors=True)
        self._sent("delete-day", self.ctas_wg,
                   f"-- deleted {len(gone)} files under {HISTORY_PREFIX}requested_day={day}/\n"
                   f"ALTER TABLE {self.db}.trips_history DROP IF EXISTS PARTITION (requested_day = '{day}')")
        return gone

    def earliest_live(self) -> datetime | None:
        if not self.table_exists("trips"):
            return None
        self._sent("query", self.wg, f"SELECT min(requested_time) AS t FROM {self.db}.trips")
        return self.con.execute("SELECT min(requested_time) FROM trips").fetchone()[0]

    def make_view(self, cutover: datetime) -> int:
        self._sent("ddl", self.ctas_wg, sql.view(self.db, cutover))
        self.con.execute(sql.view("main", cutover, history=self._history(), live="trips")
                         .replace("main.trips_all", "trips_all"))
        self._add_table("trips_all")
        self._sent("query", self.wg, sql.left_out(f"{self.db}.trips_history", cutover))
        return self.con.execute(sql.left_out(self._history(), cutover)).fetchone()[0]

    def history_rows(self, days: list[date] | None) -> list[dict]:
        where = f"WHERE requested_day IN ({', '.join(repr(str(d)) for d in days)})" if days else ""
        cur = self.con.execute(f"SELECT * FROM {self._history()} {where} ORDER BY requested_day, trip_id")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


class AthenaEngine(Engine):
    """Athena in the team's sandbox; trips_history's files under s3://<bucket>/history/trips/."""

    def __init__(self, db: str, workgroup: str, ctas_workgroup: str, sess, bucket: str, timeout_s: int = 900):
        super().__init__(db, workgroup, ctas_workgroup)
        self.sess, self.bucket, self.timeout_s = sess, bucket, timeout_s
        self.athena, self.s3, self.glue = sess.client("athena"), sess.client("s3"), sess.client("glue")
        self.location = f"s3://{bucket}/{HISTORY_PREFIX}"

    def run(self, kind: str, workgroup: str, text: str) -> list[dict]:
        self._sent(kind, workgroup, text)
        qid = self.athena.start_query_execution(QueryString=text, WorkGroup=workgroup,
                                                QueryExecutionContext={"Database": self.db})["QueryExecutionId"]
        deadline = time.time() + self.timeout_s
        while True:
            q = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() > deadline:
                self.athena.stop_query_execution(QueryExecutionId=qid)
                raise TimeoutError(f"Athena query {qid} still {state} after {self.timeout_s} s")
            time.sleep(2)
        scanned = q.get("Statistics", {}).get("DataScannedInBytes", 0)
        self.scanned += scanned
        self.sent[-1].update(query_id=qid, scanned=scanned)
        print(f"athena   {kind:<10} {workgroup:<22} {scanned:>12,} bytes scanned  {qid}")
        if state != "SUCCEEDED":
            raise RuntimeError(f"Athena {kind} {state}: {q['Status'].get('StateChangeReason')}")
        if kind not in ("query",):
            return []
        rows, token = [], None
        while True:
            page = self.athena.get_query_results(QueryExecutionId=qid, **({"NextToken": token} if token else {}))
            data = page["ResultSet"]["Rows"]
            rows += data
            token = page.get("NextToken")
            if not token:
                break
        if not rows:
            return []
        header = [c.get("VarCharValue") for c in rows[0]["Data"]]
        return [dict(zip(header, [c.get("VarCharValue") for c in r["Data"]])) for r in rows[1:]]

    def table_exists(self, name: str) -> bool:
        try:
            self.glue.get_table(DatabaseName=self.db, Name=name)
            return True
        except self.glue.exceptions.EntityNotFoundException:
            return False

    def ensure_source(self, src: Source, files: str | None = None) -> None:
        if src.kind == "files":
            made = not self.table_exists("history_raw")
            self.run("ddl", self.ctas_wg, sql.external_table(src, self.db, files))
            if made:
                self._record("history_raw")

    def count(self, src: Source, days: list[date]) -> dict[date, int]:
        rows = self.run("query", self.wg, sql.source_counts(src, self.relation(src, ATHENA), days, ATHENA))
        return {date.fromisoformat(r["day"]): int(r["n"]) for r in rows if r["day"]}

    def history_counts(self, days: list[date]) -> dict[date, int]:
        rows = self.run("query", self.wg, sql.history_counts(f"{self.db}.trips_history", days))
        return {date.fromisoformat(r["day"]): int(r["n"]) for r in rows}

    def history_source(self) -> str | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=SOURCE_KEY)["Body"].read().decode().strip()
        except self.s3.exceptions.NoSuchKey:
            return None

    def set_history_source(self, source: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=SOURCE_KEY, Body=(source + "\n").encode())

    def leftover_files(self) -> int:
        page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=HISTORY_PREFIX, MaxKeys=1)
        return page.get("KeyCount", 0)

    def _record(self, table: str) -> None:
        """A Glue table made by an Athena statement takes no tags: the ledger is how the teardown knows it is ours
        (the live teardown of 2026-11-23 found trips_history by its name only)."""
        from tags import record, recorded
        name = f"{self.db}.{table}"
        if recorded("glue-table", name) != "created":
            record("glue-table", name, "created", script="ext-backfill/backfill.py", clean="ext-backfill/clean_up.py --yes")

    def copy(self, src: Source, days: list[date], run_id: str, first: bool) -> None:
        if first:
            self.run("ctas", self.ctas_wg, sql.ctas(src, self.relation(src, ATHENA), self.db, self.location, days,
                                                    run_id))
            self._record("trips_history")
        else:
            self.run("insert", self.ctas_wg, sql.insert_into(src, self.relation(src, ATHENA), self.db, days, run_id))

    def _keys(self, prefix: str) -> list[str]:
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def day_files(self, day: date) -> list[str]:
        return [k.rsplit("/", 1)[-1] for k in self._keys(f"{HISTORY_PREFIX}requested_day={day}/")]

    def delete_day(self, day: date) -> list[str]:
        keys = self._keys(f"{HISTORY_PREFIX}requested_day={day}/")
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]],
                                                               "Quiet": True})
        self.run("delete-day", self.ctas_wg,
                 f"ALTER TABLE {self.db}.trips_history DROP IF EXISTS PARTITION (requested_day = '{day}')")
        return keys

    def earliest_live(self) -> datetime | None:
        if not self.table_exists("trips"):
            return None
        rows = self.run("query", self.wg, f"SELECT CAST(min(requested_time) AS varchar) AS t FROM {self.db}.trips")
        t = rows[0]["t"] if rows else None
        return datetime.fromisoformat(t[:19]) if t else None

    def make_view(self, cutover: datetime) -> int:
        self.run("ddl", self.ctas_wg, sql.view(self.db, cutover))
        self._record("trips_all")
        rows = self.run("query", self.wg, sql.left_out(f"{self.db}.trips_history", cutover))
        return int(rows[0]["n"]) if rows else 0

    def history_rows(self, days: list[date] | None) -> list[dict]:
        """The Parquet files of trips_history, read from S3 (no Athena query): one GET per file."""
        import pyarrow.parquet as pq

        keys = ([k for d in days for k in self._keys(f"{HISTORY_PREFIX}requested_day={d}/")] if days
                else self._keys(HISTORY_PREFIX))
        out = []
        for key in sorted(keys):
            day = key.split("requested_day=")[1].split("/")[0]
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            if not body:
                continue
            for row in pq.read_table(io.BytesIO(body)).to_pylist():
                row["requested_day"] = day
                out.append(row)
        return out
