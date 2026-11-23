"""Steps of ext-backfill (features/backfill.feature, features/trips_by_id.feature).

Offline the backfill writes under .local/ext-backfill/ of the scenario's folder: trips_history's Parquet folders,
the DuckDB file that stands in for Athena, and statements.jsonl, the Athena text of every statement with its run,
kind and workgroup. DynamoDB and S3 are moto. ImportTable is answered by the stub below: it reads the import files
from (moto) S3 and puts their items into a new moto table, so CSV imports work too, and DescribeImport reports the
counts a scenario gives.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import boto3
import duckdb
from behave import given, then, when

import support

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "ext-backfill"
SIM01 = EXT / "sim" / "SIM01-march-sample.csv"
SIM04 = EXT / "sim" / "SIM04" / "history.csv"
LOCAL = Path(".local") / "ext-backfill"
HISTORY = LOCAL / "lake" / "history" / "trips"
TABLE = "recipe-trips-by-id"
TYPES = {"trip_id": "VARCHAR", "pickup_zone_id": "BIGINT", "dropoff_zone_id": "BIGINT",
         "requested_time": "TIMESTAMP", "trip_miles": "DOUBLE", "trip_time_s": "BIGINT", "fare": "DOUBLE",
         "driver_pay": "DOUBLE", "origin": "VARCHAR", "run_id": "VARCHAR", "requested_day": "DATE"}


def ddb():
    return boto3.client("dynamodb", region_name="ca-central-1")


def s3():
    return boto3.client("s3", region_name="ca-central-1")


def backfill(context, args: str) -> None:
    context.marks = getattr(context, "marks", []) + [len(context.calls.made)]
    support.run_script(context, "ext-backfill/backfill.py", args.split())
    context.runs.append(context.last)


def statements(run: str | None = None) -> list[dict]:
    lines = [json.loads(x) for x in (LOCAL / "statements.jsonl").read_text().splitlines()]
    run = run or lines[-1]["run"]
    return [s for s in lines if s["run"] == run]


def last_run() -> list[dict]:
    lines = [json.loads(x) for x in (LOCAL / "statements.jsonl").read_text().splitlines()]
    return statements(lines[-1]["run"])


def history(where: str = "") -> list[dict]:
    files = list(HISTORY.glob("requested_day=*/*.parquet"))
    if not files:
        return []
    cur = duckdb.sql(f"SELECT * FROM read_parquet('{HISTORY}/requested_day=*/*.parquet', hive_partitioning = true, "
                     f"hive_types = {{'requested_day': DATE}}) {where} ORDER BY requested_day, trip_id")
    return [dict(zip(cur.columns, r)) for r in cur.fetchall()]


def partitions() -> dict[str, int]:
    return {d.name.split("=")[1]: len(history(f"WHERE requested_day = DATE '{d.name.split('=')[1]}'"))
            for d in sorted(HISTORY.glob("requested_day=*"))}


def day_files(day: str) -> list[str]:
    return sorted(p.name for p in (HISTORY / f"requested_day={day}").glob("*.parquet"))


def write_history(rows: list[dict]) -> None:
    """trips_history as these rows, one Parquet folder per requested_day, and registered in the local catalog."""
    shutil.rmtree(HISTORY, ignore_errors=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE h (" + ", ".join(f"{c} {t}" for c, t in TYPES.items()) + ")")
    for r in rows:
        con.execute(f"INSERT INTO h VALUES ({', '.join('?' for _ in TYPES)})",
                    [r.get(c) for c in TYPES])
    con.execute(f"COPY h TO '{HISTORY}' (FORMAT PARQUET, PARTITION_BY (requested_day), "
                f"FILENAME_PATTERN 'given_{{uuid}}')")
    con.close()
    catalog(add="trips_history")


def catalog(add: str) -> None:
    path = LOCAL / "catalog.json"
    LOCAL.mkdir(parents=True, exist_ok=True)
    cat = json.loads(path.read_text()) if path.exists() else {"tables": []}
    if add not in cat["tables"]:
        cat["tables"].append(add)
    path.write_text(json.dumps(cat))


def table_rows(table) -> list[dict]:
    out = []
    for row in table:
        r = {h: row[h] for h in table.headings}
        for k in ("pickup_zone_id", "dropoff_zone_id", "trip_time_s"):
            if k in r:
                r[k] = int(r[k])
        for k in ("fare", "trip_miles", "driver_pay"):
            if k in r:
                r[k] = float(r[k])
        if "requested_time" in r:
            r["requested_time"] = datetime.fromisoformat(r["requested_time"].replace("Z", ""))
        out.append(r)
    return out


def local_db():
    return duckdb.connect(str(LOCAL / "athena.duckdb"))


# ---- the source and trips_history

@given('the simulated data set SIM04 "team-files" is under raw/history/')
def step_sim04(context):
    Path("raw/history").mkdir(parents=True, exist_ok=True)
    shutil.copy(SIM04, "raw/history/history.csv")


@given("history.toml says format csv, time column request_datetime")
def step_history_toml(context):
    text = (EXT / "history.toml").read_text()
    assert 'format = "csv"' in text and 'time_column = "request_datetime"' in text, text


@given('the simulated data set SIM01 "march-sample" is the source')
def step_sim01(context):
    assert sum(1 for _ in open(SIM01)) == 1001      # the header line and 1,000 rows


@given("a source with trips on each day from {first} to {last}")
def step_source_days(context, first, last):
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    path = Path("six-months.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hvfhs_license_num", "request_datetime", "pickup_datetime", "pulocationid", "dolocationid",
                    "trip_miles", "trip_time", "base_passenger_fare", "driver_pay"])
        d, n = a, 0
        while d <= b:
            for zone, hour in ((48, 8), (161, 23)):
                n += 1
                t = datetime(d.year, d.month, d.day, hour, 50)
                w.writerow(["HV0003", t, t + timedelta(minutes=12), zone, 1, 2.5, 900, 10 + n % 7, 8.0])
            d += timedelta(days=1)
    import os
    os.environ["BACKFILL_SAMPLE"] = str(path.resolve())


@given('the simulated data set SIM04 "team-files" has been copied into trips_history')
def step_sim04_copied(context):
    step_sim04(context)
    backfill(context, "--source files --files raw/history/ --from 2024-03-14 --to 2024-03-15")
    assert context.exit_code == 0, context.printed
    context.first_ids = sorted(r["trip_id"] for r in history("WHERE requested_day = DATE '2024-03-14'"))


@when("day {day} is deleted and copied again")
def step_recopy(context, day):
    shutil.rmtree(HISTORY / f"requested_day={day}")
    backfill(context, "--source files --files raw/history/ --from 2024-03-14 --to 2024-03-15")


@given('ext-backfill/backfill.py has run with "{args}"')
def step_has_run(context, args):
    backfill(context, args)
    assert context.exit_code == 0, context.printed
    context.first_files = {d: day_files(d) for d in partitions()}


@given('the simulated data set SIM02 "short-day" is in trips_history')
def step_sim02(context):
    backfill(context, "--source course --from 2024-03-01 --to 2024-03-30")
    assert context.exit_code == 0, context.printed
    rows = history("WHERE requested_day = DATE '2024-03-14'")
    assert len(rows) == 34, len(rows)
    folder = HISTORY / "requested_day=2024-03-14"
    context.short_files = day_files("2024-03-14")
    shutil.rmtree(folder)
    folder.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE h (" + ", ".join(f"{c} {t}" for c, t in TYPES.items() if c != "requested_day") + ")")
    for r in rows[:29]:                     # an INSERT INTO cut short: 5 of the day's 34 rows are missing
        con.execute(f"INSERT INTO h VALUES ({', '.join('?' for c in TYPES if c != 'requested_day')})",
                    [r[c] for c in TYPES if c != "requested_day"])
    con.execute(f"COPY h TO '{folder}/short.parquet' (FORMAT PARQUET)")
    con.close()
    context.short_files = day_files("2024-03-14")


@when("the count check runs for {first} to {last}")
def step_check(context, first, last):
    backfill(context, f"--check-only --source course --from {first} --to {last}")


@given("trips_history holds these rows:")
def step_history_rows(context):
    write_history(table_rows(context.table))


@given("trips holds these rows:")
def step_trips_rows(context):
    rows = table_rows(context.table)
    make_trips([{**r, "trip_state": r.get("trip_state", "completed")} for r in rows])


def make_trips(rows: list[dict]) -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    con = local_db()
    con.execute("CREATE OR REPLACE TABLE trips (trip_id VARCHAR, trip_state VARCHAR, pickup_zone_id BIGINT, "
                "requested_time TIMESTAMP, fare DOUBLE)")
    for r in rows:
        con.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?)",
                    [r["trip_id"], r["trip_state"], r.get("pickup_zone_id"), r["requested_time"], r.get("fare")])
    con.close()
    catalog(add="trips")


@given("the events trip_requested, trip_accepted and trip_completed of trip {trip} have been merged into trips")
def step_events(context, trip):
    context.events = [
        {"event_type": "trip_requested", "event_id": "e-1", "trip_id": trip, "event_time": "2026-11-23T14:00:00Z"},
        {"event_type": "trip_accepted", "event_id": "e-2", "trip_id": trip, "event_time": "2026-11-23T14:01:00Z"},
        {"event_type": "trip_completed", "event_id": "e-3", "trip_id": trip, "event_time": "2026-11-23T14:30:00Z"}]


@given("trip {trip} was requested in zone {zone:d} with a fare of {fare}")
def step_trip_detail(context, trip, zone, fare):
    events = context.events
    events[0]["pickup_zone_id"], events[2]["fare"] = zone, float(fare)
    # what scheduled-merge keeps of a trip's three events: one row, the zone of the request, the fare of the
    # completion, the state of the newest event (a stand-in for its MERGE INTO, which runs only in Athena)
    newest = max(events, key=lambda e: e["event_time"])
    make_trips([{"trip_id": trip, "trip_state": newest["event_type"].removeprefix("trip_"),
                 "pickup_zone_id": events[0]["pickup_zone_id"], "fare": events[2]["fare"],
                 "requested_time": datetime.fromisoformat(events[0]["event_time"].replace("Z", ""))}])


@when("view trips_all is read")
def step_read_view(context):
    backfill(context, "--view-only")
    assert context.exit_code == 0, context.printed
    con = local_db()
    cur = con.execute("SELECT * FROM trips_all ORDER BY trip_id")
    context.view = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    con.close()


# ---- what Athena was sent, and what trips_history holds

@then("Athena was sent one CREATE EXTERNAL TABLE IF NOT EXISTS {name} with skip.header.line.count 1")
def step_external(context, name):
    ddl = [s for s in last_run() if s["sql"].startswith(f"CREATE EXTERNAL TABLE IF NOT EXISTS {name} ")]
    assert len(ddl) == 1, last_run()
    assert "'skip.header.line.count' = '1'" in ddl[0]["sql"] and "OpenCSVSerde" in ddl[0]["sql"], ddl[0]["sql"]


@then("every column of history_raw is a string, cast to its history.toml type in the CTAS")
def step_strings(context):
    ddl = next(s["sql"] for s in last_run() if s["sql"].startswith("CREATE EXTERNAL TABLE"))
    columns = [ln.strip().rstrip(",") for ln in ddl.split("(", 1)[1].split(")")[0].splitlines() if ln.strip()]
    assert columns and all(c.endswith(" string") for c in columns), columns
    ctas = next(s["sql"] for s in last_run() if s["kind"] == "ctas")
    for cast in ("try_cast(pulocationid AS bigint)", "try_cast(base_passenger_fare AS double)",
                 "try_cast(trip_time AS bigint)", "try(date_parse(request_datetime, '%Y-%m-%d %H:%i:%s'))"):
        assert cast in ctas, (cast, ctas)


@then("history_raw has {n:d} rows")
def step_raw_rows(context, n):
    con = local_db()
    got = con.execute("SELECT count(*) FROM history_raw").fetchone()[0]
    con.close()
    assert got == n, got


@then("trips_history has {n:d} partitions, requested_day={a} with {x:d} rows and requested_day={b} with {y:d} rows")
def step_two_partitions(context, n, a, x, b, y):
    assert partitions() == {a: x, b: y}, partitions()


@then("trips_history has {n:d} partitions, requested_day={a} to requested_day={b}")
def step_partitions_range(context, n, a, b):
    days = sorted(partitions())
    assert len(days) == n and days[0] == a and days[-1] == b, days


@then("trips_history has {n:d} partitions")
def step_partitions(context, n):
    assert len(partitions()) == n, len(partitions())


@then("Athena ran 1 CTAS and no INSERT INTO")
def step_one_ctas(context):
    kinds = [s["kind"] for s in last_run()]
    assert kinds.count("ctas") == 1 and "insert" not in kinds, kinds


@then("the CTAS went to workgroup {ctas_wg} and the counts to workgroup {wg}")
def step_workgroups(context, ctas_wg, wg):
    run = last_run()
    assert all(s["workgroup"] == ctas_wg for s in run if s["kind"] in ("ctas", "insert", "ddl")), run
    queries = [s for s in run if s["kind"] == "query"]
    assert queries and all(s["workgroup"] == wg for s in queries), queries


@then("Athena ran 1 CTAS for January and 5 INSERT INTO, one for each month from February to June")
def step_months(context):
    copies = [s for s in last_run() if s["kind"] in ("ctas", "insert")]
    assert [s["kind"] for s in copies] == ["ctas"] + ["insert"] * 5, [s["kind"] for s in copies]
    for s, month in zip(copies, range(1, 7)):
        start = f"request_datetime >= TIMESTAMP '2024-{month:02d}-01 00:00:00'"
        assert start in s["sql"], (month, s["sql"][-600:])


@then('trips_history has {n:d} distinct trip_id values')
def step_distinct(context, n):
    ids = [r["trip_id"] for r in history()]
    assert len(ids) == n and len(set(ids)) == n, ids


@then('the two identical rows have trip_ids that differ only in their ends, "{a}" and "{b}"')
def step_identical(context, a, b):
    rows = history()
    key = lambda r: tuple(v for k, v in r.items() if k not in ("trip_id",))   # noqa: E731
    twins = [r for r in rows if sum(1 for s in rows if key(s) == key(r)) == 2]
    ids = sorted(r["trip_id"] for r in twins)
    assert len(ids) == 2 and ids[0].endswith(a) and ids[1].endswith(b), ids
    assert ids[0][:-len(a)] == ids[1][:-len(b)], ids


@then('every row has origin "{origin}" and run_id "{run_id}"')
def step_origin(context, origin, run_id):
    rows = history()
    assert rows and all(r["origin"] == origin and r["run_id"] == run_id for r in rows), rows[:2]


@then("the {n:d} trip_id values of requested_day={day} are the ones the first copy wrote")
def step_same_ids(context, n, day):
    ids = sorted(r["trip_id"] for r in history(f"WHERE requested_day = DATE '{day}'"))
    assert len(ids) == n and ids == context.first_ids, (ids, context.first_ids)


@then("Athena ran 1 INSERT INTO, for the {n:d} days {a} to {b}")
def step_one_insert(context, n, a, b):
    copies = [s for s in last_run() if s["kind"] in ("ctas", "insert")]
    assert [s["kind"] for s in copies] == ["insert"], copies
    end = date.fromisoformat(b) + timedelta(days=1)
    assert f">= TIMESTAMP '{a} 00:00:00'" in copies[0]["sql"] and f"< TIMESTAMP '{end} 00:00:00'" in copies[0]["sql"]


@then("the files of requested_day={a} to requested_day={b} are the ones the first run wrote")
def step_files_kept(context, a, b):
    for d, files in context.first_files.items():
        if a <= d <= b:
            assert day_files(d) == files, (d, day_files(d), files)


@then("the files under history/trips/requested_day={day}/ were deleted before the day was copied again")
def step_deleted_first(context, day):
    run = last_run()
    kinds = [(s["kind"], s["sql"]) for s in run]
    delete = next(i for i, (k, t) in enumerate(kinds) if k == "delete-day" and f"requested_day = '{day}'" in t)
    insert = next(i for i, (k, t) in enumerate(kinds) if k == "insert" and f"DATE '{day}'" in t or
                  k == "insert" and f"TIMESTAMP '{day} 00:00:00'" in t)
    assert delete < insert, kinds
    assert not set(day_files(day)) & set(context.short_files), (day_files(day), context.short_files)


@then("the count of {day} in trips_history is {n:d}")
def step_day_count(context, day, n):
    assert partitions()[day] == n, partitions()


@then('trips_all has {n:d} rows: {a} with origin "{oa}" and {b} with origin "{ob}"')
def step_view_rows(context, n, a, oa, b, ob):
    con = local_db()
    rows = con.execute("SELECT trip_id, origin FROM trips_all ORDER BY trip_id").fetchall()
    con.close()
    assert sorted(rows) == sorted([(a, oa), (b, ob)]) and len(rows) == n, rows


@then('it has 1 row for {trip}, with pickup_zone_id {zone:d}, fare {fare} and origin "{origin}"')
def step_view_trip(context, trip, zone, fare, origin):
    rows = [r for r in context.view if r["trip_id"] == trip]
    assert len(rows) == 1, context.view
    r = rows[0]
    assert r["pickup_zone_id"] == zone and abs(r["fare"] - float(fare)) < 1e-9 and r["origin"] == origin, r


# ---- @live

@given("the course catalog is registered as the data source course")
def step_live_catalog(context):
    athena = boto3.client("athena", region_name="ca-central-1")
    athena.get_data_catalog(Name="course")


@then("trips_history counts {a:d}, {b:d} and {c:d} trips for zones 48, 161 and 230")
def step_live_counts(context, a, b, c):
    import sys
    sys.path.insert(0, str(EXT))
    from engines import AthenaEngine
    import os
    sess = boto3.session.Session(region_name="ca-central-1")
    account = sess.client("sts").get_caller_identity()["Account"]
    p = os.environ["PREFIX"]
    eng = AthenaEngine(p.replace("-", "_") + "_lake", f"{p}-wg", f"{p}-ctas-wg", sess, f"{p}-lake-{account}")
    rows = eng.run("query", f"{p}-wg", "SELECT pickup_zone_id AS z, count(*) AS n FROM trips_history "
                                        "WHERE requested_day BETWEEN DATE '2024-03-01' AND DATE '2024-03-30' GROUP BY 1")
    got = {int(r["z"]): int(r["n"]) for r in rows}
    assert got == {48: a, 161: b, 230: c}, got


# ---- DynamoDB

class ImportStub:
    """ImportTable and DescribeImport, played: the files under the prefix become a moto table."""

    def __init__(self):
        self.report: dict | None = None
        self.desc: dict = {}

    def import_table(self, params):
        src, fmt = params["S3BucketSource"], params["InputFormat"]
        create = params["TableCreationParameters"]
        client = s3()
        items = []
        for o in client.list_objects_v2(Bucket=src["S3Bucket"], Prefix=src["S3KeyPrefix"]).get("Contents", []):
            body = client.get_object(Bucket=src["S3Bucket"], Key=o["Key"])["Body"].read()
            text = gzip.decompress(body).decode() if params.get("InputCompressionType") == "GZIP" else body.decode()
            if fmt == "DYNAMODB_JSON":
                items += [json.loads(line)["Item"] for line in text.splitlines() if line.strip()]
            else:       # CSV: every non-key column is imported as a string
                items += [{k: {"S": v} for k, v in row.items() if v != ""}
                          for row in csv.DictReader(io.StringIO(text))]
        d = ddb()
        d.create_table(TableName=create["TableName"], KeySchema=create["KeySchema"],
                       AttributeDefinitions=create["AttributeDefinitions"], BillingMode=create["BillingMode"])
        for it in items:
            d.put_item(TableName=create["TableName"], Item=it)
        arn = f"arn:aws:dynamodb:ca-central-1:{support.ACCOUNT}:table/{create['TableName']}/import/01"
        report = self.report or {"ImportedItemCount": len(items), "ErrorCount": 0}
        self.desc = {"ImportArn": arn, "ImportStatus": "COMPLETED", "InputFormat": fmt,
                     "TableArn": f"arn:aws:dynamodb:ca-central-1:{support.ACCOUNT}:table/{create['TableName']}",
                     **report}
        return {"ImportTableDescription": {"ImportArn": arn, "ImportStatus": "IN_PROGRESS", "InputFormat": fmt}}

    def describe_import(self, params):
        return {"ImportTableDescription": self.desc}


@given("table {table} does not exist")
def step_no_table(context, table):
    assert table not in ddb().list_tables()["TableNames"]
    context.import_stub = ImportStub()
    context.calls.answer("ImportTable", context.import_stub.import_table)
    context.calls.answer("DescribeImport", context.import_stub.describe_import)


@given('trips_history holds the simulated data set SIM01 "march-sample" of {n:d} rows')
def step_history_sim01(context, n):
    backfill(context, "--source course --from 2024-03-01 --to 2024-03-30")
    assert context.exit_code == 0 and len(history()) == n, context.printed
    context.printed = ""


@given("the import reports ImportedItemCount {imported:d} and ErrorCount {errors:d}")
def step_import_report(context, imported, errors):
    context.import_stub.report = {"ImportedItemCount": imported, "ErrorCount": errors}


def create_table() -> None:
    ddb().create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                       AttributeDefinitions=[{"AttributeName": "trip_id", "AttributeType": "S"}],
                       KeySchema=[{"AttributeName": "trip_id", "KeyType": "HASH"}])


def sim03_rows() -> list[dict]:
    """60 trips of SIM01's sample: the first 60 rows of trips_history after a copy of SIM01."""
    return history()[:60]


@given("table {table} exists")
def step_table_exists(context, table):
    create_table()


@given("table {table} exists and holds {n:d} live items")
def step_table_live(context, table, n):
    create_table()
    for i in range(n):
        ddb().put_item(TableName=table, Item={"trip_id": {"S": f"t-{i:04d}"}, "origin": {"S": "live"}})


def _sim03(context):
    backfill(context, "--source course --from 2024-03-01 --to 2024-03-30")
    rows = sim03_rows()
    write_history(rows)
    context.history_ids = [r["trip_id"] for r in history()]
    context.printed = ""


@given('trips_history holds 60 rows of the simulated data set SIM03 "throttled-writes" without its fault')
def step_sim03_clean(context):
    _sim03(context)


@given('the simulated data set SIM03 "throttled-writes" returns 5 unprocessed items per call on {when}')
def step_sim03(context, when):
    _sim03(context)
    times = 2 if when == "the first 2 attempts" else -1

    def answer(params):
        (table, requests), = params["RequestItems"].items()
        keep, left = requests[:-5], requests[-5:]
        for r in keep:
            ddb().put_item(TableName=table, Item=r["PutRequest"]["Item"])
        return {"UnprocessedItems": {table: left}}

    context.calls.answer("BatchWriteItem", answer, times=times)


@then("the files under imports/trips-by-id/ hold {n:d} lines, each an Item in DynamoDB JSON")
def step_import_files(context, n):
    client, bucket = s3(), f"recipe-lake-{support.ACCOUNT}"
    lines = []
    for o in client.list_objects_v2(Bucket=bucket, Prefix="imports/trips-by-id/")["Contents"]:
        lines += gzip.decompress(client.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()).decode().splitlines()
    assert len(lines) == n, len(lines)
    for line in lines:
        item = json.loads(line)["Item"]
        assert all(len(v) == 1 and next(iter(v)) in ("S", "N") for v in item.values()), item


@then("one ImportTable call with input format {fmt} created {table} with key {key}")
def step_import_call(context, fmt, table, key):
    calls = context.calls.of("ImportTable")
    assert len(calls) == 1 and calls[0]["InputFormat"] == fmt, calls
    create = calls[0]["TableCreationParameters"]
    assert create["TableName"] == table and create["KeySchema"] == [{"AttributeName": key, "KeyType": "HASH"}]
    assert table in ddb().list_tables()["TableNames"]


@then("no BatchWriteItem call was made")
def step_no_batch(context):
    assert not context.calls.of("BatchWriteItem")


def item(trip: str) -> dict:
    return ddb().get_item(TableName=TABLE, Key={"trip_id": {"S": trip}}, ConsistentRead=True)["Item"]


@then("item {trip} has fare {fare} as a number")
def step_fare_number(context, trip, fare):
    v = item(trip)["fare"]
    assert "N" in v and Decimal(v["N"]) == Decimal(fare), v


@then("an UpdateItem that adds {add} to fare leaves {total}")
def step_add(context, add, total):
    got = ddb().update_item(TableName=TABLE, Key={"trip_id": {"S": "h-01a2-1"}}, UpdateExpression="ADD fare :a",
                            ExpressionAttributeValues={":a": {"N": add}}, ReturnValues="UPDATED_NEW")
    assert Decimal(got["Attributes"]["fare"]["N"]) == Decimal(total), got


@then('item {trip} has fare "{fare}" as text')
def step_fare_text(context, trip, fare):
    v = item(trip)["fare"]
    assert "S" in v and Decimal(v["S"]) == Decimal(fare), v


@then("every BatchWriteItem call carried at most 25 items, each trip_id once")
def step_batches(context):
    calls = context.calls.of("BatchWriteItem")
    assert calls
    for c in calls:
        (reqs,) = c["RequestItems"].values()
        keys = [r["PutRequest"]["Item"]["trip_id"]["S"] for r in reqs]
        assert len(keys) <= 25 and len(set(keys)) == len(keys), keys


@then("table {table} has {n:d} items")
def step_items(context, table, n):
    got = ddb().scan(TableName=table, Select="COUNT", ConsistentRead=True)["Count"]
    assert got == n, got


@then("the script waited twice, under limits of {a} s and {b} s")
def step_waits(context, a, b):
    waits = support.WAITS
    assert len(waits) == 2 and 0 <= waits[0] < float(a) and 0 <= waits[1] < float(b), waits


@then('the run stops with an error that holds "{text}" and the first unprocessed trip_id')
def step_unprocessed(context, text):
    first = context.history_ids[20]          # the first call carries 25 items; the stand-in leaves its last 5
    assert context.exit_code == 1 and text in context.printed and first in context.printed, context.printed[-600:]


@then("the count scan read every page with ConsistentRead and counted the items of load_id {load_id}")
def step_scan(context, load_id):
    scans = context.calls.of("Scan")
    assert scans, context.calls.names()
    for s in scans:
        assert s["ConsistentRead"] is True and s["Select"] == "COUNT", s
        assert s["ExpressionAttributeValues"][":l"] == {"S": load_id} and "#l" in s["FilterExpression"], s
