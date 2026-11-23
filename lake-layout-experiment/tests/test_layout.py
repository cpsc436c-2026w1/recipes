"""Offline tests of the lake layout experiment: the statements Athena gets, the spend bound, billed MB, the local
stand-in end to end, the DuckDB function's code on local files, the REPORT parser, and the clean-up in moto."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import boto3
from moto import mock_aws

import layout as L
from backends import AthenaBackend, LocalBackend
from report_lines import join_by_request, parse_report
from sim_data import write_sim01
from store import connect, load_prices, store_athena, summary


@pytest.fixture(autouse=True)
def offline_env(monkeypatch, tmp_path):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "PREFIX": "test", "LEDGER": str(tmp_path / "ledger.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def local(tmp_path):
    b = LocalBackend(tmp_path / "local")
    b.extract_ds01(write_sim01(tmp_path / "sim01.parquet"))
    return b


# --- statements -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("layout, must", [
    ("hive-flat", ["format = 'PARQUET'", "write_compression = 'ZSTD'", "external_location = 's3://b/l/'"]),
    ("hive-by-day", ["partitioned_by = ARRAY['dt']", "external_location"]),
    ("iceberg-flat", ["table_type = 'ICEBERG'", "is_external = false", "location = 's3://b/l/'", "'ZSTD'"]),
    ("iceberg-by-day", ["partitioning = ARRAY['day(pickup_time)']", "table_type = 'ICEBERG'"]),
])
def test_ctas_properties(layout, must):
    [(kind, sql, a, b)] = L.build_statements("db", layout, 2, "s3://b/l/")
    assert kind == "CTAS" and sql.startswith(f"CREATE TABLE db.{L.table(layout, 2)} WITH (")
    if layout in L.BY_DAY:
        assert "FROM UNNEST(sequence(1, 2)) AS c(copy_no) CROSS JOIN db.ds01" in sql  # copies first (see below)
        assert sql.index("c.copy_no AS copy_no") < sql.index(" AS dt FROM")  # dt last, as Hive requires
    else:  # a flat table of more than 1 copy is written from the Hive by-day table of its size
        assert sql.endswith("SELECT " + ", ".join(L.COLUMNS) + " FROM db.trips_hive_day_2")
    for m in must:
        assert m in sql
    if layout.endswith("flat"):
        assert "partition" not in sql


def test_flat_dated_table_is_one_ctas():
    assert [k for k, *_ in L.build_statements("db", "hive-flat", 4, "s3://b/", dated=True)] == ["CTAS"]


def test_dated_copies_move_by_31_days():
    sql = L.build_statements("db", "iceberg-by-day", 4, "s3://b/", dated=True)[1][1]
    assert sql.startswith("INSERT INTO db.trips_ice_day_4 SELECT date_add('day', 31 * (c.copy_no - 1), pickup_time)")
    assert "BETWEEN DATE '2024-06-09' AND DATE '2024-07-02'" in sql


def test_each_layout_gets_its_own_filter():
    assert "WHERE dt = DATE '2024-03-14'" in L.query_sql("db", "hive-by-day", 1, "one-day")
    assert "dt BETWEEN DATE '2024-03-11' AND DATE '2024-03-17'" in L.query_sql("db", "hive-by-day", 1, "one-week")
    assert ("pickup_time >= TIMESTAMP '2024-03-14 00:00:00' AND pickup_time < TIMESTAMP '2024-03-15 00:00:00'"
            in L.query_sql("db", "iceberg-by-day", 1, "one-day"))
    assert "pickup_time < TIMESTAMP '2024-03-18 00:00:00'" in L.query_sql("db", "hive-flat", 1, "one-week")
    assert "WHERE" not in L.query_sql("db", "iceberg-flat", 1, "whole-table")


# --- growth and the bound -----------------------------------------------------------------------------------------

def test_bound_counts_every_column_of_every_file_a_query_may_read():
    per = {lay: L.TableBytes(total=200 * L.MB, day=6.6 * L.MB, week=46 * L.MB) for lay in L.LAYOUTS}
    one = L.plan_upper_bound_usd([1], per, 5.50)
    # 10 runs x (flat: 3 queries x 200 MB, x2 layouts) + by-day: (10 MB min + 46 MB + 200 MB) x 2 layouts
    mb = 10 * (2 * 3 * 200 + 2 * (10 + 46 + 200))
    assert one == pytest.approx(mb * L.MB / L.TB * 5.50)


def test_growth_caps_copies_for_a_tiny_table():
    g = L.grow(lambda k: 0.001, lambda sizes: 0.0, 2.0, max_copies=4)
    assert g.sizes == [1, 2, 4] and "capped at 4" in g.note


@pytest.mark.parametrize("measured, note", [
    # The first live build (one-day query, by-day tables, MB): the copies of a row written side by side
    ({1: 0.16, 2: 0.28, 4: 0.29}, "(0.28 MB at 2 copies, 0.29 MB at 4 copies)"),
    # The second: each copy one run of DS01's rows, the same rows on the same day (still little more room than one copy)
    ({1: 0.16, 2: 0.32, 4: 0.60, 8: 1.02, 16: 1.12}, "(1.02 MB at 8 copies, 1.12 MB at 16 copies)"),
])
def test_growth_stops_when_the_copies_compress_away(measured, note):
    g = L.grow(lambda k: measured[k], lambda sizes: 0.01, 2.0)
    assert g.sizes == [] and f"did not grow with the copies {note}" in g.note
    assert sorted(g.probes) == sorted(measured)  # stopped at the first doubling that did not grow


def test_copies_rotate_the_days_of_march(tmp_path):
    # Copy n holds, on day d, the trips of day d + n - 1 (after the 31st, from the 1st): no day holds the same rows
    # twice, the rows are n times DS01's, and dt stays the day of pickup_time.
    import duckdb

    src = tmp_path / "ds.parquet"
    duckdb.execute(f"""COPY (SELECT TIMESTAMP '2024-03-01 08:00:00' + to_days(CAST(d AS INTEGER)) AS pickup_time,
        CAST(d AS INTEGER) AS pickup_zone_id, 1 AS dropoff_zone_id, 1.0 AS trip_miles, 1 AS trip_time_s, 1.0 AS fare,
        1.0 AS driver_pay, CAST(DATE '2024-03-01' + to_days(CAST(d AS INTEGER)) AS DATE) AS dt
        FROM range(31) t(d), range(d + 1) u(r)) TO '{src}' (FORMAT parquet)""")  # day d+1 has d+1 trips in zone d
    b = LocalBackend(tmp_path / "local")
    b.extract_ds01(src)
    b.build_table("hive-by-day", 4)
    rows = b.run_query("hive-by-day", 4, "one-day")["rows"]  # 2024-03-14: zones 13, 14, 15, 16 (days 14 to 17)
    assert rows == [(13, 14), (14, 15), (15, 16), (16, 17)]
    t = b._read("hive-by-day", 4, "whole-table")
    assert duckdb.execute(f"SELECT count(*), count(*) FILTER (WHERE CAST(pickup_time AS DATE) <> dt), "
                          f"min(dt), max(dt) FROM {t}").fetchone() == (4 * 496, 0, date(2024, 3, 1), date(2024, 3, 31))
    # the 31st of copy 2 holds the 1st's trips
    assert duckdb.execute(f"SELECT pickup_zone_id FROM {t} WHERE copy_no = 2 AND dt = DATE '2024-03-31'").fetchall() == [(0,)]


def test_athena_copies_rotate_with_the_same_expression():
    sql = L.build_statements("db", "hive-by-day", 4, "s3://b/")[0][1]
    assert f"date_add('day', {L.rotation_days()}, dt) AS dt" in sql
    assert f"date_add('day', {L.rotation_days()}, pickup_time) AS pickup_time" in sql


def test_copies_are_written_one_after_the_other_so_bytes_grow_with_them(tmp_path):
    # The same order offline: 4 copies of a table of distinct rows take about 4 times the zone column's bytes.
    import duckdb

    src = tmp_path / "ds.parquet"
    duckdb.execute(f"""COPY (SELECT TIMESTAMP '2024-03-14 00:00:00' + to_seconds(i) AS pickup_time,
        CAST(hash(i) % 260 AS INTEGER) AS pickup_zone_id, 1 AS dropoff_zone_id, 1.0 AS trip_miles, 1 AS trip_time_s,
        1.0 AS fare, 1.0 AS driver_pay, DATE '2024-03-14' AS dt FROM range(200000) t(i)) TO '{src}' (FORMAT parquet)""")
    b = LocalBackend(tmp_path / "local")
    b.extract_ds01(src)
    for k in (1, 4):
        b.build_table("hive-flat", k)
    size = {k: duckdb.execute(f"SELECT sum(total_compressed_size) FROM parquet_metadata("
                              f"'{tmp_path}/local/layout/trips_hive_flat_{k}/*.parquet') "
                              f"WHERE path_in_schema = 'pickup_zone_id'").fetchone()[0] for k in (1, 4)}
    assert size[4] > 3 * size[1], size


def test_a_stopped_ctas_leaves_files_that_the_next_build_deletes(monkeypatch):
    from backends import AthenaBackend
    from names import Names

    class S3:
        def __init__(self):
            self.keys, self.deleted = ["layout/trips_hive_day_256/dt=2024-03-01/f1"], []

        def get_paginator(self, _):
            keys = self.keys

            class P:
                def paginate(self, Bucket, Prefix):
                    return [{"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}]
            return P()

        def delete_objects(self, Bucket, Delete):
            self.deleted += [o["Key"] for o in Delete["Objects"]]

    b = AthenaBackend.__new__(AthenaBackend)
    b.n, b.sess, b.s3 = Names(prefix="recipe-t"), _Sess(set()), S3()
    b.n.__dict__["bucket"] = "bkt"
    monkeypatch.setattr(b, "must", lambda sql, *a, **k: {})
    b.build_table("hive-by-day", 256)
    assert b.s3.deleted == ["layout/trips_hive_day_256/dt=2024-03-01/f1"]


def test_growth_probes_each_doubling_once():
    asked = []
    L.grow(lambda k: asked.append(k) or k * 3.1, lambda s: 0.1, 2.0)
    assert asked == [1, 2, 4]


# --- billed and excluded --------------------------------------------------------------------------------------------

def test_billed_and_spend_rules():
    assert L.billed_mb(3 * L.MB) == 10 and L.billed_mb(125829121) == 121
    assert L.billed_mb(0, reused=True) == 0 and L.billed_mb(5 * L.MB, "FAILED") == 0
    assert L.spend_billed_mb("FAILED", 1024 * L.MB, False, "Query cancelled! : Bytes scanned limit was exceeded") == 1024
    assert L.exclusion("FAILED", False, "Query cancelled! : Bytes scanned limit was exceeded") == "scan cutoff"
    assert L.exclusion("FAILED", False, "SYNTAX_ERROR") == "failed" and L.exclusion("SUCCEEDED", False) == ""


def test_run_plan_has_90_duckdb_calls_on_hive_by_day():
    plan = L.run_plan([1, 4, 16])
    duck = [r for r in plan if r.engine == "duckdb"]
    assert len(plan) == 450 and len(duck) == 90 and {r.layout for r in duck} == {"hive-by-day"}


def test_costs_per_1000():
    p = {"lambda_arm64_gb_s": 0.0000133334, "lambda_request": 0.0000002, "s3_get_per_1000": 0.00044}
    assert round(L.athena_cost_per_1000(10, 5.50), 4) == 0.0525
    assert round(L.lambda_cost_per_1000(850, 2048, 40, p), 4) == 0.0405


# --- REPORT lines ---------------------------------------------------------------------------------------------------

def test_report_line_cold_and_warm():
    cold = parse_report("START RequestId: rq-1\nREPORT RequestId: rq-1\tDuration: 849.20 ms\tBilled Duration: 2750 ms"
                        "\tMemory Size: 2048 MB\tMax Memory Used: 410 MB\tInit Duration: 1900.10 ms\t\n")
    warm = parse_report("REPORT RequestId: rq-2\tDuration: 419.60 ms\tBilled Duration: 420 ms\tMemory Size: 2048 MB"
                        "\tMax Memory Used: 412 MB\t")
    assert cold["init_duration_ms"] == 1900.10 and warm["init_duration_ms"] is None
    assert L.billed_gb_s(cold["billed_duration_ms"], 2048) == 5.5 and L.billed_gb_s(420, 2048) == 0.84
    joined = join_by_request([{"request_id": "rq-2", "rows": 3}], [cold, warm])
    assert joined[0]["billed_duration_ms"] == 420 and joined[0]["rows"] == 3
    assert parse_report("no report here") is None


# --- the local stand-in end to end ----------------------------------------------------------------------------------

def test_offline_experiment_end_to_end(tmp_path, monkeypatch):
    import experiment

    monkeypatch.setattr(experiment, "LOCAL_ROOT", tmp_path / "local")
    monkeypatch.setattr(experiment, "STATE", tmp_path / "state" / "experiment.json")
    assert experiment.main(["build", "--offline"]) == 0
    # the offline build keeps its sizes next to its results: the live build's state/experiment.json is not touched
    # (before, an offline build between the live build and the live run would have replaced the live sizes)
    assert not (tmp_path / "state" / "experiment.json").exists()
    state = json.loads((tmp_path / "local" / "experiment.json").read_text())
    assert state["sizes"] == [1, 2, 4] and state["note"].startswith("10 MB not reached")
    assert experiment.main(["run", "--offline"]) == 0
    con = connect(tmp_path / "local" / "results.duckdb")
    assert con.execute("SELECT count(*) FROM athena_runs").fetchone()[0] == 360
    assert con.execute("SELECT count(*) FROM duckdb_runs").fetchone()[0] == 90
    assert con.execute("SELECT count(*) FROM duckdb_runs WHERE cold").fetchone()[0] == 1
    con.close()
    assert experiment.main(["summary", "--offline"]) == 0
    text = (tmp_path / "local" / "summary.md").read_text()
    assert text.count("\n| ") == 1 + 36 + 9 and "results differ" not in text
    assert experiment.main(["late", "--offline"]) == 0
    assert experiment.main(["clean", "--offline", "--yes"]) == 0
    assert not (tmp_path / "local" / "layout").exists()


def test_by_day_prunes_folders_and_flat_does_not(local):
    for lay in L.LAYOUTS:
        local.build_table(lay, 2)
    day = {lay: local.run_query(lay, 2, "one-day") for lay in L.LAYOUTS}
    assert len({tuple(d["rows"]) for d in day.values()}) == 1 and dict(day["hive-flat"]["rows"]) == {161: 2, 230: 3}
    # copy 2 holds the 15th's trips on the 14th (the days rotate: test_copies_rotate_the_days_of_march)
    assert day["hive-by-day"]["data_scanned_bytes"] < day["hive-flat"]["data_scanned_bytes"]
    assert local.count("iceberg-by-day", 2) == 10


def test_dated_copies_offline_span_more_days(local):
    local.build_table("hive-by-day", 2, dated=True)
    assert local.folders("hive-by-day", 2) == ["dt=2024-03-14", "dt=2024-03-15", "dt=2024-04-14", "dt=2024-04-15"]


def test_duckdb_function_code_on_local_files(local):
    import handler

    local.build_table("hive-by-day", 1)
    folder = str(local._folder("hive-by-day", 1))
    out = handler.run("one-day", folder, con=handler.connect(local=True))
    assert out["rows"] == [[161, 2], [230, 1]]
    assert handler.run("whole-table", folder, con=handler.connect(local=True))["rows"] == [[161, 2], [230, 3]]


def test_summary_counts_excluded_runs_in_the_spend(tmp_path):
    con = connect(tmp_path / "r.duckdb")
    load_prices(con)
    r = L.Run("athena", "hive-flat", 1, "whole-table", 1)
    store_athena(con, "t", r, query_execution_id="a", state="SUCCEEDED", data_scanned_bytes=20 * L.MB, to_last_row_ms=1)
    store_athena(con, "t", L.Run("athena", "hive-flat", 1, "whole-table", 2), query_execution_id="b", state="FAILED",
                 reason="Query cancelled! : Bytes scanned limit was exceeded", data_scanned_bytes=1024 * L.MB,
                 to_last_row_ms=9)
    cols, rows = summary(con)
    c = dict(zip(cols, rows[0]))
    assert c["runs_kept"] == 1 and c["spend_mb"] == 20 + 1024 and c["price_date"] == "2026-11-18"


# --- live code paths in moto ----------------------------------------------------------------------------------------

def test_table_bytes_from_s3_listing():
    import boto3

    from names import Names

    with mock_aws():
        sess = boto3.Session(region_name="ca-central-1")
        n = Names(sess=sess)
        s3 = sess.client("s3")
        s3.create_bucket(Bucket=n.bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
        for d, size in (("2024-03-13", 100), ("2024-03-14", 300), ("2024-03-15", 200)):
            s3.put_object(Bucket=n.bucket, Key=f"layout/trips_hive_day_2/dt={d}/x.parquet", Body=b"x" * size)
        b = AthenaBackend(n, sess).table_bytes("hive-by-day", 2)
        assert (b.total, b.day, b.week) == (300, 150, 300)


def test_course_catalog_is_recorded_borrowed_even_when_registered():
    import boto3
    from tags import recorded

    from names import Names

    with mock_aws():
        sess = boto3.Session(region_name="ca-central-1")
        assert AthenaBackend(Names(sess=sess), sess).ensure_course_catalog("111122223333") == "registered"
        assert recorded("athena-data-source", "course") == "borrowed"


def test_duckdb_role_policy_reads_layout_only():
    import function

    class N:
        bucket, log_group = "test-lake-111122223333", "/aws/lambda/test-duckdb-query"

    doc = function.role_policy(N, "111122223333")
    actions = sorted(a for s in doc["Statement"] for a in s["Action"])
    assert actions == ["logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:ListBucket"]
    assert doc["Statement"][0]["Resource"].endswith("/layout/*")


def test_clean_up_dry_run_contacts_nothing(monkeypatch):
    import clean_up

    monkeypatch.setattr(clean_up.boto3, "Session", lambda **k: (_ for _ in ()).throw(AssertionError("AWS")))

    class N:
        database, function, log_group, role = "test_layout", "test-duckdb-query", "/aws/lambda/x", "test-duckdb-role"

    steps = clean_up.clean_up(N, yes=False)
    assert steps.dry_run and len(steps.removed) == 5


assert date and Path


def test_summary_reads_only_the_latest_run(tmp_path):
    con = connect(tmp_path / "r.duckdb")
    load_prices(con)
    for rid, ms in (("20261201T000000Z", 5000.0), ("20261202T000000Z", 1000.0)):
        for n in (1, 2):
            store_athena(con, rid, L.Run("athena", "hive-flat", 1, "one-day", n), query_execution_id=f"{rid}-{n}",
                         state="SUCCEEDED", data_scanned_bytes=L.MB, to_last_row_ms=ms)
    cols, rows = summary(con)
    c = dict(zip(cols, rows[0]))
    assert c["runs"] == 2 and c["median_ms"] == 1000.0


def test_a_failed_call_is_an_excluded_run_not_the_end(tmp_path, local):
    import experiment

    for lay in L.LAYOUTS:
        local.build_table(lay, 1)
    calls = {"n": 0}
    real = local.run_query

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 3:
            raise TimeoutError("still running")
        return real(*a)

    local.run_query = flaky
    con = connect(tmp_path / "r.duckdb")
    assert experiment.run(local, [1], con, duckdb_engine=False, log=lambda *a: None) == 120
    got = con.execute("SELECT count(*), count(*) FILTER (WHERE excluded) FROM athena_runs").fetchone()
    assert got == (120, 1)


def test_function_glob_reads_files_without_an_extension(tmp_path):
    import duckdb
    import handler

    d = tmp_path / "t" / "dt=2024-03-14"
    d.mkdir(parents=True)
    duckdb.execute(f"COPY (SELECT 161 AS pickup_zone_id) TO '{d / '20261201_000000_00001_abcd'}' (FORMAT parquet)")
    assert handler.run("one-day", str(tmp_path / "t"), con=handler.connect(local=True))["rows"] == [[161, 1]]


def test_ds01_ctas_casts_the_pickup_time_to_milliseconds():
    # The first live build stopped: "Incorrect timestamp precision for timestamp(6); the configured precision is
    # MILLISECONDS; column name: pickup_time" (the course's Iceberg table keeps microseconds; Hive CTAS writes ms).
    from layout import ds01_ctas

    sql = ds01_ctas("recipe_layout", "s3://b/layout/")
    assert "CAST(r.pickup_datetime AS timestamp(3)) AS pickup_time" in sql
    assert "external_location = 's3://b/layout/ds01/'" in sql
    assert "r.pickup_datetime >= TIMESTAMP '2024-03-01 00:00:00'" in sql and "'HV0003'" in sql


class _Glue:
    class exceptions:
        class EntityNotFoundException(Exception):
            pass

    def __init__(self, tables):
        self.tables = set(tables)

    def get_table(self, DatabaseName, Name):
        if Name not in self.tables:
            raise self.exceptions.EntityNotFoundException(Name)
        return {"Table": {"Name": Name}}


class _Sess:
    def __init__(self, tables):
        self.glue = _Glue(tables)

    def client(self, name, **_):
        return self.glue if name == "glue" else None


def test_build_run_again_keeps_the_tables_it_has(monkeypatch):
    # A build that stopped part-way (the first live build did) is run again: tables that exist are not built twice
    # (a second CTAS into the same location fails), the others are.
    from backends import AthenaBackend
    from names import Names

    b = AthenaBackend.__new__(AthenaBackend)
    b.n, b.sess = Names(prefix="recipe-t"), _Sess({"trips_hive_day_1"})
    b.n.__dict__["bucket"] = "bkt"
    monkeypatch.setattr(b, "empty_location", lambda prefix: 0, raising=False)
    ran = []
    monkeypatch.setattr(b, "must", lambda sql, *a, **k: ran.append(sql) or {})
    assert b.build_table("hive-by-day", 1)[0].endswith("(kept from an earlier build)") and ran == []
    assert b.build_table("iceberg-by-day", 1) == ["CTAS"] and len(ran) == 1


def _dry_lines(step: str) -> str:
    import os
    import subprocess

    env = {**os.environ, "DRY": "1", "STEP": step}
    return subprocess.run(["bash", str(Path(__file__).resolve().parent.parent / "run.sh")], env=env,
                          capture_output=True, text=True, check=True).stdout


def _dry(step: str) -> list[str]:
    import os
    import subprocess

    env = {**os.environ, "DRY": "1", "STEP": step}
    out = subprocess.run(["bash", str(Path(__file__).resolve().parent.parent / "run.sh")], env=env,
                         capture_output=True, text=True, check=True).stdout
    return [line.split("experiment.py ")[-1].split()[0] for line in out.splitlines() if "experiment.py" in line]


def test_run_sh_build_then_run_does_not_build_twice_and_plans_the_clean_up_first():
    # The live run went STEP=build, then the rest: the second part must not build again (the tables exist), and the
    # clean-up prints its plan before it deletes.
    assert _dry("build") == ["build"]
    # the @live scenario counts DS01's rows in the tables of 1 copy, so it runs before late adds its row (the live run
    # had them the other way round, and the scenario failed on 6,045,944 rows)
    out = _dry_lines("run")
    assert out.index("behave -t @live") < out.index("experiment.py late")
    assert _dry("run") == ["run", "summary", "late", "clean", "clean"]
    assert _dry("")[0] == "build"


def test_extension_download_names_its_client(monkeypatch):
    # The live build's package step got 403 from extensions.duckdb.org with urllib's default User-Agent.
    import function

    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"x"

    def urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return R()

    monkeypatch.setattr(function.urllib.request, "urlopen", urlopen)
    assert function.download(function.HTTPFS) == b"x"
    assert seen["ua"] == function.USER_AGENT and not seen["ua"].startswith("Python-urllib")


def test_package_makes_its_folder(tmp_path, monkeypatch):
    # The live build had no build/ folder (only an offline run makes it): the zip could not be opened.
    import gzip
    import zipfile

    import function

    monkeypatch.setattr(function.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(function, "download", lambda url: gzip.compress(b"ext"))
    out = function.package(tmp_path / "new" / "duckdb-query.zip")
    assert sorted(zipfile.ZipFile(out).namelist()) == ["handler.py", "httpfs.duckdb_extension"]


def test_function_counts_object_gets_and_listings_from_the_http_log():
    # DuckDB 2.0.1's JSON profile names no request count (s3_gets was null in the live smoke call); its HTTP log
    # has every request. The rows below are the shape of the log of one one-day query (measured 2026-11-23).
    import importlib.util

    spec = importlib.util.spec_from_file_location("fn_handler", Path(__file__).resolve().parent.parent
                                                  / "duckdb_fn" / "handler.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    host = "https://b.s3.ca-central-1.amazonaws.com"
    rows = [("GET", f"{host}/?redacted"), ("GET", f"{host}/layout/t/dt%3D2024-03-01/f0"),
            ("GET", f"{host}/layout/t/dt%3D2024-03-14/f1"), ("GET", f"{host}/layout/t/dt%3D2024-03-14/f1"),
            ("HEAD", f"{host}/layout/t/dt%3D2024-03-14/f1")]
    assert h.requests(rows) == {"s3_gets": 3, "s3_lists": 1}


def test_flat_tables_of_one_copy_come_from_ds01_and_by_day_tables_are_built_first():
    assert "CROSS JOIN db.ds01" in L.build_statements("db", "hive-flat", 1, "s3://b/")[0][1]
    assert L.build_order() == ["hive-by-day", "iceberg-by-day", "hive-flat", "iceberg-flat"]


def test_scaling_check_catches_the_flat_tables_of_the_live_run():
    # Whole-table Data scanned (MB) of the third live build: the by-day tables grew with the copies, the flat ones
    # (written from DS01 x copies) did not.
    scanned = {("hive-by-day", 1): 4.88, ("hive-by-day", 8): 39.03, ("hive-by-day", 32): 153.75,
               ("hive-flat", 1): 4.88, ("hive-flat", 8): 6.73, ("hive-flat", 32): 12.37}
    wrong = L.scaling_problems(scanned, [1, 8, 32])
    assert len(wrong) == 2 and all(w.startswith("trips_hive_flat_") for w in wrong)
    assert "trips_hive_flat_8 scans 6.73 MB for the whole table, 1.4 times 1 copy (4.88 MB), not about 8" in wrong
    assert L.scaling_problems({("hive-flat", 1): 0.001, ("hive-flat", 4): 0.001}, [1, 4]) == []  # offline SIM01


def test_clean_up_bare_run_lists_the_real_tables_and_objects_and_deletes_nothing():
    """With AWS access the bare run names each table of the database and counts the objects under layout/, so a
    student can check they are the experiment's before --yes; nothing is deleted."""
    import clean_up
    import lake
    from names import Names

    with mock_aws():
        sess = boto3.Session(region_name="ca-central-1")
        lake.up(sess, log=lambda *_: None)
        n = Names(prefix="recipe-test", sess=sess)
        glue = sess.client("glue")
        glue.create_database(DatabaseInput={"Name": n.database})
        for t in ("trips_hive_day_1", "trips_iceberg_flat_1"):
            glue.create_table(DatabaseName=n.database, TableInput={"Name": t})
        sess.client("s3").put_object(Bucket=n.bucket, Key="layout/x/part-0.parquet", Body=b"x")
        steps = clean_up.clean_up(n, yes=False, sess=sess)
        assert f"glue table {n.database}.trips_hive_day_1" in steps.removed
        assert f"s3://{n.bucket}/layout/ (1 objects)" in steps.removed
        assert len(glue.get_tables(DatabaseName=n.database)["TableList"]) == 2          # nothing deleted
        assert sess.client("s3").list_objects_v2(Bucket=n.bucket, Prefix="layout/")["KeyCount"] == 1


def test_clean_up_bare_run_after_the_bucket_is_gone_lists_without_failing():
    import clean_up
    from names import Names

    with mock_aws():
        sess = boto3.Session(region_name="ca-central-1")
        n = Names(prefix="recipe-test", sess=sess)
        steps = clean_up.clean_up(n, yes=False, sess=sess)
        assert not steps.failed and not any("layout/" in x for x in steps.removed)
