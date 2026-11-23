"""A recording stand-in for Athena, for ext-maintenance's nightly run offline (moto has no query engine).

It writes down every statement and answers from what a scenario states: the data files per day of an Iceberg
table (`"<table>$partitions"`), the rows of a day of trip_events_parquet, the rows a CTAS writes, the rows counted after
the switch, an OPTIMIZE it refuses. A CTAS puts three small objects under its external_location, as a real one
writes files there; the count after the switch reads, from Glue, where the day's partition points.
"""
from __future__ import annotations

import re

import compact_days as cd


class StubAthena:
    def __init__(self, s3=None, glue=None, bucket: str = ""):
        self.s3, self.glue, self.bucket = s3, glue, bucket
        self.sent: list[tuple[str, str]] = []            # (workgroup, statement)
        self.spent: list[dict] = []
        self.files: dict[str, dict[str, int]] = {}       # "db.table" -> {day or "whole table": files}
        self.day_rows: dict[str, int] = {}
        self.ctas_rows: dict[str, int] = {}
        self.after_rows: dict[str, int] = {}
        self.refuse: set[str] = set()                    # tables whose OPTIMIZE ... WHERE is refused
        self.missing: set[str] = set()                   # tables Athena does not find
        self.zones: list[dict] = []

    def statements(self, start: str = "") -> list[str]:
        return [sql for _, sql in self.sent if sql.startswith(start)]

    def run(self, sql: str, workgroup: str, database: str | None = None) -> list[dict]:
        self.sent.append((workgroup, sql))
        self.spent.append({"sql": sql[:80], "state": "SUCCEEDED", "scanned": 10 * 2 ** 20, "ms": 900})
        day = (re.search(r"dt = '(\d{4}-\d{2}-\d{2})'", sql) or [None, None])[1]
        if sql.startswith("OPTIMIZE"):
            table = sql.split()[1]
            if "WHERE" in sql and table.split(".")[1] in self.refuse:
                self.spent[-1]["state"] = "FAILED"
                raise cd.AthenaError("FAILED: GENERIC_INTERNAL_ERROR: Unexpected FilterNode found in plan; probably "
                                     "connector was not able to handle provided WHERE expression (Athena's text for a "
                                     "WHERE that is not on partition columns; put in by the scenario)")
            return []
        m = re.search(r'FROM "([^"]+)"\."([^"$]+)\$(files|partitions)"', sql)
        if m and m.group(2) in self.missing:
            self.spent[-1]["state"] = "FAILED"
            raise cd.AthenaError(f"FAILED: TABLE_NOT_FOUND: Table '{m.group(1)}.{m.group(2)}' does not exist")
        if m:
            counts = self.files.get(f"{m.group(1)}.{m.group(2)}", {})
            if m.group(3) == "partitions":
                return [{"day": d, "files": str(n)} for d, n in sorted(counts.items()) if d != "whole table"]
            return [{"files": str(counts.get("whole table", 0))}]
        if sql.startswith("CREATE TABLE"):
            location = re.search(r"external_location = '([^']+)'", sql).group(1)
            b, prefix = cd.key_of(location)
            for i in range(3):
                self.s3.put_object(Bucket=b, Key=f"{prefix}bucket-{i:05d}.zstd.parquet", Body=b"PAR1")
            return []
        if "count_if" in sql:
            location = cd.partition_location(self.glue, *database_table(sql), day)
            n = self.after_rows.get(day, self.ctas_rows.get(day, self.day_rows.get(day, 0)))
            return [{"n": str(n), "outside": "0" if cd.COMPACTED in location else str(n)}]
        m = re.search(r"FROM \S+\.compact_(\d{8})_", sql)
        if m:
            d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
            return [{"n": str(self.ctas_rows.get(d, self.day_rows.get(d, 0)))}]
        if "count(*) AS n" in sql and day:
            return [{"n": str(self.day_rows.get(day, 0))}]
        if "GROUP BY 1" in sql and "pickup_zone_id" in sql:
            return [dict(z) for z in self.zones]
        return []


def database_table(sql: str) -> tuple[str, str]:
    db, table = re.search(r"FROM (\w+)\.(\w+) WHERE dt", sql).groups()
    return db, table
