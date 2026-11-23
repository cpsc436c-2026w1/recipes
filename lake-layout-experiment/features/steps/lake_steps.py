"""Steps of features/lake_layout.feature. The numbers in the tables are the scenarios' inputs: the growth rule, the
statements, the run plan and summary.sql are run on them as they are, with no AWS call (moto for the clean-up)."""

from __future__ import annotations

import json
import re
from datetime import date

from behave import given, then, when

from layout import (BY_DAY, LAYOUTS, MB, QUERIES, REUSE_OFF, Run, build_statements, grow, run_plan, table)
from store import connect, load_prices, store_athena, store_duckdb, store_rows, summary


def _names():
    from names import Names

    return Names()


def _fill(text: str) -> str:
    n = _names()
    return text.replace("<PREFIX>", n.prefix).replace("<prefix>", n.glue_prefix)


def _sizes(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


def _con(context):
    if getattr(context, "con", None) is None:
        context.con = connect(context.tmp / "results.duckdb")
        load_prices(context.con)
    return context.con


def _cells(context) -> list[dict]:
    cols, rows = summary(_con(context))
    context.summary_cols = cols
    return [dict(zip(cols, r)) for r in rows]


def _cell(context, engine, layout, copies, query) -> dict:
    return next(c for c in context.cells if (c["engine"], c["layout"], c["copies"], c["query"]) ==
                (engine, layout, copies, query))


# ---- R10 ----

@given('the simulated data set SIM01 "two-days"')
def sim01(context):
    from backends import LocalBackend
    from sim_data import write_sim01

    context.backend = LocalBackend(context.tmp / "local")
    context.rows = context.backend.extract_ds01(write_sim01(context.tmp / "sim01.parquet"))


@when("experiment.py build writes the size of 1 copy from SIM01")
def build_one(context):
    for layout in LAYOUTS:
        context.backend.build_table(layout, 1)


@then("the tables trips_hive_flat_1, trips_hive_day_1, trips_ice_flat_1 and trips_ice_day_1 each have "
      "{n:d} rows and the same {c:d} columns")
def same_rows(context, n, c):
    cols = {lay: context.backend.columns(lay, 1) for lay in LAYOUTS}
    counts = {lay: context.backend.count(lay, 1) for lay in LAYOUTS}
    assert all(c == n for c in counts.values()), counts  # after experiment.py late each has one row more
    assert len({tuple(v) for v in cols.values()}) == 1 and len(cols["hive-flat"]) == c, cols


@then("trips_hive_day_1 has 2 folders, dt=2024-03-14 and dt=2024-03-15")
def hive_folders(context):
    assert context.backend.folders("hive-by-day", 1) == ["dt=2024-03-14", "dt=2024-03-15"]


@then("trips_ice_day_1 has 2 partitions, for 2024-03-14 and 2024-03-15")
def ice_partitions(context):
    assert context.backend.partitions("iceberg-by-day", 1) == ["2024-03-14", "2024-03-15"]


@then("trips_hive_flat_1 and trips_ice_flat_1 have 0 partitions")
def flat_partitions(context):
    assert context.backend.partitions("hive-flat", 1) == context.backend.partitions("iceberg-flat", 1) == []


@given('the course catalog is registered as the data source "course"')
def live_catalog(context):
    import os

    from experiment import live_backend

    context.backend, context.names, context.sess, lake = live_backend()
    lake.up(context.sess)
    for wg in (context.names.ctas_wg, context.names.wg):
        lake.ensure_workgroup(wg, sess=context.sess)
    context.backend.ensure_course_catalog(os.environ["COURSE_CATALOG_ID"])
    context.backend.check_source_columns()


@when("experiment.py build runs")
def live_build(context):
    context.rows = context.backend.extract_ds01()
    for layout in LAYOUTS:
        context.backend.build_table(layout, 1)


@then("the table ds01 has {n:d} rows")
def ds01_rows(context, n):
    assert context.rows == n, context.rows


@then("each of the four tables of 1 copy has {n:d} rows")
def four_tables(context, n):
    counts = {lay: context.backend.count(lay, 1) for lay in LAYOUTS}
    assert all(c == n for c in counts.values()), counts  # after experiment.py late each has one row more


# ---- R20, R30 ----

@given("MAX_USD is {usd:g}")
def max_usd(context, usd):
    context.max_usd, context.probes, context.bounds = usd, {}, {}


@given("the probe and the upper bound of the plan a doubling would lead to are:")
def probe_table(context):
    for r in context.table:
        context.probes[int(r["copies"])] = float(r["probe_mb"])
        if r["plan_if_doubled"]:
            context.bounds[_sizes(r["plan_if_doubled"])] = float(r["plan_upper_bound_usd"])


@given("the upper bound of the plan {plan} copies, computed again before the final tables, is {usd:g} USD")
@given("the upper bound of the plan {plan} copies, computed before the final tables, is {usd:g} USD")
def final_bound(context, plan, usd):
    context.bounds[_sizes(plan)] = usd


@given("at 1 copy the probe is {mb:g} MB and the upper bound of the plan {plan} copies is {usd:g} USD")
@given("the probe at 1 copy is {mb:g} MB and the upper bound of the plan {plan} copies is {usd:g} USD")
def one_copy(context, mb, plan, usd):
    context.probes[1] = mb
    context.bounds[_sizes(plan)] = usd


@when("the build grows the table")
def grows(context):
    asked = []

    def probe(k):
        asked.append(k)
        return context.probes[k]

    context.growth = grow(probe, lambda sizes: context.bounds[tuple(sizes)], context.max_usd)
    context.asked = asked
    context.plan = run_plan(context.growth.sizes) if context.growth.sizes else []


@then("the sizes are {plan} copies")
def sizes_are(context, plan):
    assert tuple(context.growth.sizes) == _sizes(plan), context.growth


@then('the summary says "{text}"')
def summary_says(context, text):
    assert context.growth.note == text, context.growth.note


@then("no final table is built and the run plan has 0 runs")
def no_plan(context):
    assert context.growth.sizes == [] and context.plan == []


# ---- R40 ----

@given("4 dated copies of DS01, from 2024-03-01 to 2024-07-02, 124 days")
def dated(context):
    context.dated_copies = 4


@when("the build statements of trips_hive_day_4 are planned")
def planned(context):
    context.statements = build_statements("db", "hive-by-day", context.dated_copies, "s3://b/layout/x/", dated=True)


@then("they are:")
def they_are(context):
    got = [(k, str(a), str(b)) for k, _sql, a, b in context.statements]
    want = [(r["statement"], r["first_day"], r["last_day"]) for r in context.table]
    assert got == want, got
    assert context.statements[0][1].startswith("CREATE TABLE db.trips_hive_day_4 WITH (")
    assert "partitioned_by = ARRAY['dt']" in context.statements[0][1]


# ---- R50 ----

@given("4 layouts, 3 sizes and the queries one-day, one-week and whole-table")
def plan_inputs(context):
    context.sizes = [1, 4, 16]


@when("experiment.py run plans its Athena runs with seed 436")
def plans(context):
    context.plan = [r for r in run_plan(context.sizes, 436) if r.engine == "athena"]


@then("the plan has 360 runs, 10 for each of 36 cells, all in workgroup <PREFIX>-wg")
def plan_has(context):
    from collections import Counter

    cells = Counter(r.cell for r in context.plan)
    assert len(context.plan) == 360 and len(cells) == 36 and set(cells.values()) == {10}
    assert _names().wg == f"{_names().prefix}-wg"  # AthenaBackend.run_query sends every run to names.wg


@then("every run asks Athena not to reuse results")
def reuse_off(context):
    assert REUSE_OFF == {"ResultReuseByAgeConfiguration": {"Enabled": False}}


@then("planning again with seed 436 gives the same order")
def same_order(context):
    assert [r for r in run_plan(context.sizes, 436) if r.engine == "athena"] == context.plan
    assert [r for r in run_plan(context.sizes, 437) if r.engine == "athena"] != context.plan


# ---- R60, R70 ----

@given("Athena reports these finished runs:")
def finished_runs(context):
    context.athena_rows = [dict(r.items()) for r in context.table]


@given("these runs of the cell hive-by-day, 16 copies, whole-table:")
def runs_of_cell(context):
    con = _con(context)
    for i, r in enumerate(context.table, 1):
        store_athena(con, "t", Run("athena", "hive-by-day", 16, "whole-table", i),
                     query_execution_id=r["query_execution_id"], state=r["state"],
                     reused=r["reused_previous_result"] == "true", data_scanned_bytes=int(r["data_scanned_bytes"]),
                     to_last_row_ms=float(r["to_last_row_ms"]), engine_ms=float(r["to_last_row_ms"]))


@when("the runs are stored")
def runs_stored(context):
    con = _con(context)
    for i, r in enumerate(getattr(context, "athena_rows", []), 1):
        store_athena(con, "t", Run("athena", "hive-flat", 1, "one-day", i), query_execution_id=r["query_execution_id"],
                     state="SUCCEEDED", data_scanned_bytes=int(r["data_scanned_bytes"]),
                     engine_ms=float(r["engine_ms"]), queue_ms=float(r["queue_ms"]),
                     planning_ms=float(r["planning_ms"]), to_last_row_ms=float(r["to_last_row_ms"]))
    for i, r in enumerate(getattr(context, "reports", []), 1):
        store_duckdb(con, "t", Run("duckdb", "hive-by-day", 1, "one-day", i), request_id=r["request_id"],
                     duration_ms=float(r["duration_ms"]), billed_duration_ms=float(r["billed_duration_ms"]),
                     init_duration_ms=float(r["init_duration_ms"]) if r["init_duration_ms"] else None,
                     memory_mb=context.memory_mb, s3_gets=None, result_rows=0, to_last_row_ms=float(r["duration_ms"]))


@then("results.duckdb has q-0001 with billed MB 10 and q-0002 with billed MB 121")
def billed(context):
    got = dict(_con(context).execute("SELECT query_execution_id, billed_mb FROM athena_runs").fetchall())
    assert got == {"q-0001": 10, "q-0002": 121}, got


@then("each has its time to the last row beside its engine, queue and planning times")
def times_beside(context):
    rows = _con(context).execute("SELECT to_last_row_ms, engine_ms, queue_ms, planning_ms FROM athena_runs").fetchall()
    assert all(None not in r for r in rows) and len(rows) == 2


@when("the summary is written")
@when("the summary is written, with quantiles interpolated between neighbouring runs")
def summary_written(context):
    context.cells = _cells(context)


@then('q-0102 is marked excluded with "result reused" and q-0103 with "scan cutoff"')
def excluded(context):
    got = dict(_con(context).execute("SELECT query_execution_id, reason FROM athena_runs WHERE excluded").fetchall())
    assert got == {"q-0102": "result reused", "q-0103": "scan cutoff"}, got


@then("the cell's times count {n:d} run")
def times_count(context, n):
    assert _cell(context, "athena", "hive-by-day", 16, "whole-table")["runs_kept"] == n


@then("the cell's spend counts {mb:d} MB of Data scanned")
def spend(context, mb):
    assert _cell(context, "athena", "hive-by-day", 16, "whole-table")["spend_mb"] == mb


# ---- R80, R90 ----

@given("the 10 kept runs of the cell hive-flat, 1 copy, one-day have these times to the last row in ms:")
def ten_runs(context):
    con = _con(context)
    for i, r in enumerate(context.table, 1):
        store_athena(con, "t", Run("athena", "hive-flat", 1, "one-day", i), query_execution_id=f"q-{i}",
                     state="SUCCEEDED", data_scanned_bytes=3 * MB, to_last_row_ms=float(r["to_last_row_ms"]),
                     engine_ms=float(r["to_last_row_ms"]) - 100)


@then("the cell's time to the last row is median {m:g} ms (IQR {a:g} to {b:g}), p90 {p:g} ms")
def quantiles(context, m, a, b, p):
    c = _cell(context, "athena", "hive-flat", 1, "one-day")
    assert (c["median_ms"], c["q25_ms"], c["q75_ms"], c["p90_ms"]) == (m, a, b, p), c


@then("the cell's row has the median Data scanned and the median billed MB in adjacent columns")
def adjacent(context):
    cols = context.summary_cols
    assert cols.index("median_billed_mb") == cols.index("median_scanned_mb") + 1
    c = _cell(context, "athena", "hive-flat", 1, "one-day")
    assert (c["median_scanned_mb"], c["median_billed_mb"]) == (3.0, 10.0)


@given("these IQRs of the time to the last row, in ms:")
def iqrs(context):
    """Two kept runs a cell, a and b, give quantile_cont 0.25 = a + (b - a)/4 and 0.75 = a + 3(b - a)/4."""
    con = _con(context)
    for r in context.table:
        layout, size, query = [x.strip() for x in r["cell"].split(",")]
        k = int(size.split()[0])
        q25, q75 = float(r["q25"]), float(r["q75"])
        d = 2 * (q75 - q25)
        for i, v in enumerate((q25 - d / 4, q25 - d / 4 + d), 1):
            store_athena(con, "t", Run("athena", layout, k, query, i), query_execution_id=f"{layout}-{k}-{i}",
                         state="SUCCEEDED", data_scanned_bytes=MB, to_last_row_ms=v, engine_ms=v)


def _overlaps(context, k) -> bool:
    c = _cell(context, "athena", "hive-flat", k, "one-day")
    return "athena hive-by-day" in (c["ranges_overlap_with"] or "")


@then('the pair at 1 copy is marked "ranges overlap"')
def pair_marked(context):
    assert _overlaps(context, 1)


@then("the pair at 16 copies is not marked")
def pair_not_marked(context):
    assert not _overlaps(context, 16)


# ---- R100 ----

@given("the function <PREFIX>-duckdb-query has {mb:d} MB")
def function_memory(context, mb):
    context.memory_mb = mb


@given("its REPORT lines for two calls of one-day on trips_hive_day_1 are:")
def reports(context):
    from report_lines import parse_report

    context.reports = []
    for r in context.table:
        line = (f"REPORT RequestId: {r['request_id']}\tDuration: {r['duration_ms']} ms\tBilled Duration: "
                f"{r['billed_duration_ms']} ms\tMemory Size: {context.memory_mb} MB\tMax Memory Used: 400 MB"
                + (f"\tInit Duration: {r['init_duration_ms']} ms" if r["init_duration_ms"] else ""))
        p = parse_report(line)
        context.reports.append({k: ("" if v is None else str(v)) for k, v in p.items()})


@then("{rq} is stored as {kind} with {gbs:g} billed GB-seconds")
def stored_as(context, rq, kind, gbs):
    cold, got = _con(context).execute("SELECT cold, billed_gb_s FROM duckdb_runs WHERE request_id = ?", [rq]).fetchone()
    assert cold == (kind == "cold") and got == gbs, (cold, got)


# ---- R110, R120 ----

def _store_result(context, engine, rows, **duck):
    con = _con(context)
    r = Run(engine, "hive-by-day", 1, "one-day", 1)
    if engine == "athena":
        store_athena(con, "t", r, query_execution_id="q-a", state="SUCCEEDED", data_scanned_bytes=duck.get("scanned", MB),
                     to_last_row_ms=900.0, engine_ms=800.0)
    else:
        store_duckdb(con, "t", r, request_id="rq-a", duration_ms=duck.get("ms", 800.0),
                     billed_duration_ms=duck.get("ms", 800.0), init_duration_ms=None,
                     memory_mb=duck.get("mem", 2048), s3_gets=duck.get("gets", 40), result_rows=len(rows),
                     to_last_row_ms=900.0)
    store_rows(con, "t", r, engine, rows)


@given("Athena returns zone {z1:d} with {n1:d} trips and zone {z2:d} with {n2:d} for one-day on trips_hive_day_1")
def athena_returns(context, z1, n1, z2, n2):
    _store_result(context, "athena", [(z1, n1), (z2, n2)])


@given("DuckDB returns zone {z1:d} with {n1:d} trips and zone {z2:d} with {n2:d} for the same query")
def duckdb_returns(context, z1, n1, z2, n2):
    _store_result(context, "duckdb", [(z1, n1), (z2, n2)])


@then('the DuckDB cell is marked "results differ" and does not get a cost comparison')
def differ(context):
    c = _cell(context, "duckdb", "hive-by-day", 1, "one-day")
    assert c["mark"] == "results differ" and c["cost_per_1000_usd"] is None, c


@then("the DuckDB cell is not marked")
def not_differ(context):
    c = _cell(context, "duckdb", "hive-by-day", 1, "one-day")
    assert c["mark"] is None and c["cost_per_1000_usd"] is not None, c


@given("the Athena cell's median billed MB is 10")
def athena_cell(context):
    _store_result(context, "athena", [(161, 1)], scanned=4 * MB)


@given("the DuckDB cell's medians are {ms:d} ms billed at {mem:d} MB and {gets:d} S3 GET requests")
def duckdb_cell(context, ms, mem, gets):
    _store_result(context, "duckdb", [(161, 1)], ms=float(ms), mem=mem, gets=gets)


@then("1,000 queries cost {a:g} USD in the Athena cell and {d:g} USD in the DuckDB cell")
def costs(context, a, d):
    context.cells = _cells(context)
    got = (_cell(context, "athena", "hive-by-day", 1, "one-day")["cost_per_1000_usd"],
           _cell(context, "duckdb", "hive-by-day", 1, "one-day")["cost_per_1000_usd"])
    assert got == (a, d), got


# ---- R130 ----

@given('the simulated data set SIM01 "two-days" is built at 1 copy')
def built_at_1(context):
    sim01(context)
    build_one(context)


@given("the one-day count of zone 161 on 2024-03-14 is {n:d} in all four tables")
@then("the one-day count of zone 161 on 2024-03-14 is {n:d} in all four tables")
def count_161(context, n):
    got = {lay: context.backend.one_day_count(lay, 1, 161) for lay in LAYOUTS}
    assert set(got.values()) == {n}, got


@when('experiment.py late inserts the simulated data set SIM02 "late-row"')
def late_row(context):
    from experiment import SIM02

    context.backend.late_insert(SIM02, 1)
    assert context.backend.snapshots("iceberg-by-day", 1) == 2  # Iceberg: one more snapshot


# ---- R140 ----

def _session():
    import boto3

    return boto3.Session(region_name="ca-central-1")


def _build_made(context):
    """What build and run leave: the database and its tables, layout/ objects, the function, its log group and role,
    and results.duckdb."""
    import function as fn

    s, n = context.sess, context.names
    glue = s.client("glue")
    glue.create_database(DatabaseInput={"Name": n.database})
    for name in ("ds01", table("hive-flat", 1), table("iceberg-by-day", 1)):
        glue.create_table(DatabaseName=n.database, TableInput={"Name": name})
    s3 = s.client("s3")
    for key in ("layout/ds01/a.parquet", "layout/trips_hive_day_1/dt=2024-03-14/b.parquet",
                "athena-results/q-1.csv"):
        s3.put_object(Bucket=n.bucket, Key=key, Body=b"x")
    iam = s.client("iam")
    arn = iam.create_role(RoleName=n.role, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))["Role"]["Arn"]
    iam.put_role_policy(RoleName=n.role, PolicyName="read-layout",
                        PolicyDocument=json.dumps(fn.role_policy(n, "123456789012")))
    s.client("logs").create_log_group(logGroupName=n.log_group)
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("handler.py", "def handler(e, c):\n    return {}\n")
    s.client("lambda").create_function(FunctionName=n.function, Runtime="python3.12", Role=arn,
                                       Handler="handler.handler", Code={"ZipFile": buf.getvalue()})
    context.results_file = context.tmp / "results.duckdb"
    context.results_file.write_bytes(b"")


def _setup(context):
    from names import Names

    context.sess = _session()
    context.names = Names(sess=context.sess)


@given("the bucket <PREFIX>-lake-<account>, the data source course, <PREFIX>-ctas-wg and <PREFIX>-wg existed "
       "before experiment.py build")
def existed_before(context):
    _setup(context)
    s, n = context.sess, context.names
    s.client("s3").create_bucket(Bucket=n.bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    athena = s.client("athena")
    athena.create_data_catalog(Name="course", Type="GLUE", Parameters={"catalog-id": "111122223333"})
    for wg in (n.ctas_wg, n.wg):
        athena.create_work_group(Name=wg, Configuration={})


@given("experiment.py build and experiment.py run have finished")
def built_and_ran(context):
    from backends import AthenaBackend

    import lake

    lake.up(context.sess, log=lambda *a: None)
    AthenaBackend(context.names, context.sess).ensure_course_catalog("111122223333")
    _build_made(context)


@given("experiment.py build ran scripts/lake.py up for the bucket <PREFIX>-lake-<account>, registered the data "
       "source course and created <PREFIX>-ctas-wg and <PREFIX>-wg")
def build_made_all(context):
    import lake

    from backends import AthenaBackend

    _setup(context)
    lake.up(context.sess, log=lambda *a: None)
    for wg in (context.names.ctas_wg, context.names.wg):
        lake.ensure_workgroup(wg, sess=context.sess)
    AthenaBackend(context.names, context.sess).ensure_course_catalog("111122223333")
    _build_made(context)


@given("the ledger records the two workgroups created and course borrowed")
def ledger_says(context):
    from tags import recorded

    n = context.names
    assert recorded("athena-workgroup", n.ctas_wg) == recorded("athena-workgroup", n.wg) == "created"
    assert recorded("athena-data-source", "course") == "borrowed"


@when("experiment.py clean --yes runs")
def clean_runs(context):
    from clean_up import clean_up

    context.steps = clean_up(context.names, yes=True, sess=context.sess)
    assert not context.steps.failed, context.steps.failed


@then("the database <prefix>_layout, the prefix layout/, <PREFIX>-duckdb-query, its log group and "
      "<PREFIX>-duckdb-role are gone")
def all_gone(context):
    s, n = context.sess, context.names
    assert s.client("glue").get_databases()["DatabaseList"] == []
    keys = [o["Key"] for o in s.client("s3").list_objects_v2(Bucket=n.bucket).get("Contents", [])]
    assert not any(k.startswith("layout/") for k in keys), keys
    assert s.client("lambda").list_functions()["Functions"] == []
    assert s.client("logs").describe_log_groups(logGroupNamePrefix=n.log_group)["logGroups"] == []
    assert s.client("iam").list_roles()["Roles"] == []


def _still_there(context, results: bool):
    s, n = context.sess, context.names
    s.client("s3").head_bucket(Bucket=n.bucket)
    athena = s.client("athena")
    assert "course" in [c["CatalogName"] for c in athena.list_data_catalogs()["DataCatalogsSummary"]]
    for wg in (n.ctas_wg, n.wg):
        athena.get_work_group(WorkGroup=wg)
    if results:
        assert context.results_file.exists()


@then("the bucket, the data source course, the two workgroups and results.duckdb are still there")
def kept_all(context):
    _still_there(context, True)


@then("the bucket, the data source course, <PREFIX>-ctas-wg and <PREFIX>-wg are still there")
def kept_wgs(context):
    _still_there(context, False)


@when("scripts/lake.py down runs after every recipe's clean-up")
def lake_down(context):
    import lake

    context.down = lake.down(context.sess, yes=True, log=lambda *a: None)


@then("<PREFIX>-ctas-wg and <PREFIX>-wg are gone")
def wgs_gone(context):
    names = [w["Name"] for w in context.sess.client("athena").list_work_groups()["WorkGroups"]]
    assert context.names.ctas_wg not in names and context.names.wg not in names, names
    assert context.down == 0


assert BY_DAY and QUERIES and date and _fill


@when("experiment.py clean runs without --yes")
def clean_bare(context):
    from clean_up import clean_up

    context.steps = clean_up(context.names, yes=False, sess=context.sess)


@then("it lists each table of <prefix>_layout and the objects under layout/")
def bare_lists(context):
    n = context.names
    tables = [t["Name"] for t in context.sess.client("glue").get_tables(DatabaseName=n.database)["TableList"]]
    assert tables and all(f"glue table {n.database}.{t}" in context.steps.removed for t in tables), context.steps.removed
    assert any(x.startswith(f"s3://{n.bucket}/layout/ (") for x in context.steps.removed), context.steps.removed


@then("the database <prefix>_layout and the prefix layout/ are still there")
def bare_kept(context):
    n = context.names
    context.sess.client("glue").get_database(Name=n.database)
    assert context.sess.client("s3").list_objects_v2(Bucket=n.bucket, Prefix="layout/")["KeyCount"] > 0

