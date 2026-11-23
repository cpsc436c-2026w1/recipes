"""Steps of features/compaction.feature and features/on_call.feature (ext-maintenance, revised), and of the
revised schedule scenario (R113). Offline: moto for S3, Glue, DynamoDB, CloudWatch and SNS; tests/athena_stub.py
for Athena; the night's time is fixed by the scenario."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from behave import given, then, when

NIGHT = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
ROWS_PER_FILE = 12


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def days(first: str, last: str) -> list[str]:
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


class Lake:
    """The lake bucket, the raw table, the run log and the Athena stand-in of one scenario."""

    def __init__(self, context):
        import boto3
        import compact_days as cd
        import lake
        import nightly_ops
        import run_log
        from athena_stub import StubAthena

        self.cd = cd
        sess = boto3.session.Session(region_name="ca-central-1")
        lake.up(sess, log=lambda *_: None)
        run_log.ensure_run_log(sess)
        self.bucket = lake.bucket_name(sess)
        self.s3, self.glue = sess.client("s3"), sess.client("glue")
        self.table = sess.resource("dynamodb").Table(run_log.run_log_name())
        nightly_ops.ensure_raw_table(self.glue, cd.Config(bucket=self.bucket), self.bucket)
        self.db, self.raw = cd.Config().raw_table
        self.athena = StubAthena(self.s3, self.glue, self.bucket)
        self.items: list[dict] = []
        self.after_list = None
        self.marks: dict = {}
        old = nightly_ops.clock
        nightly_ops.clock = lambda: NIGHT
        context.add_cleanup(setattr, nightly_ops, "clock", old)

    def cfg(self):
        return self.cd.Config(bucket=self.bucket)

    def land(self, day: str, n: int, start: int = 0, register: bool = True) -> list[str]:
        """n small files under raw/trip-events/dt=<day>/ (written straight into moto's store: 43,200 PUTs through
        boto3 would take minutes), and the day's rows for the stand-in's counts."""
        from moto.core import DEFAULT_ACCOUNT_ID
        from moto.s3.models import s3_backends
        backend = s3_backends[DEFAULT_ACCOUNT_ID]["aws"]
        keys = [f"raw/trip-events/dt={day}/{day.replace('-', '')}T{(i // 60) % 24:02d}{i % 60:02d}-{i:05d}.parquet"
                for i in range(start, start + n)]
        for key in keys:
            backend.put_object(self.bucket, key, b"PAR1")
        self.athena.day_rows[day] = self.athena.day_rows.get(day, 0) + n * ROWS_PER_FILE
        if register:
            self.cd.register(self.glue, self.db, self.raw, self.bucket, [day])
        return keys

    def night(self, now: datetime = NIGHT, context=None) -> dict | None:
        try:
            item = self.cd.nightly(now, self.cfg(), s3=self.s3, glue=self.glue, table=self.table,
                                   athena=self.athena, log=lambda *_: None, after_list=self.after_list)
        except Exception as err:                       # noqa: BLE001  the Then names the error it expects
            self.error = err
            item = self.cd.Log(self.table).get(self.cd.iso(now))
        else:
            self.error = None
        self.items.append(item)
        return item

    def location(self, day: str) -> str | None:
        return self.cd.partition_location(self.glue, self.db, self.raw, day)

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.cd.listing(self.s3, self.bucket, prefix))


def lake_of(context) -> Lake:
    if not getattr(context, "lake", None):
        if getattr(context, "live", False):            # @live: AWS itself (features/live.py)
            import live
            context.lake = live.LiveLake(context, NIGHT)
        else:
            context.lake = Lake(context)
    return context.lake


def sim02(lake: Lake) -> None:
    """SIM02 three-days: 1,440 files of 12 events on 2026-11-21, 80 on 2026-11-22, 600 on 2026-11-23 (today)."""
    lake.land("2026-11-21", 1440)
    lake.land("2026-11-22", 80)
    lake.land("2026-11-23", 600)


# ---- data sets ------------------------------------------------------------------------------------------------

def live_parquet_set(lake, data_set: str) -> None:
    """The data sets of the @live examples, in AWS (features/live.py LiveLake.land_set)."""
    import live
    sim02_plan = {"2026-11-21": 1440, "2026-11-22": 80, "2026-11-23": 600}
    if data_set == "SIM01":
        plan, rows = live.thirty_days(date(2026, 11, 23))
        lake.land_set("SIM01 thirty-days", plan, rows)
        return
    lake.land_set("SIM02 three-days", sim02_plan, live.twelve_rows)
    if data_set == "SIM05":
        lake.night(utc("2026-11-23T11:05:00Z"))
        assert lake.location("2026-11-21").startswith(f"s3://{lake.bucket}/compacted/"), lake.printed
        lake.marks["late"] = lake.land("2026-11-21", 1, start=1440, register=False)[0]
    else:
        assert data_set == "SIM02", data_set


@given('the simulated data set {data_set} "{name}" is in trip_events_parquet')
def step_parquet_set(context, data_set, name):
    lake = lake_of(context)
    if getattr(context, "live", False):
        live_parquet_set(lake, data_set)
        return
    if data_set == "SIM01":
        assert name == "thirty-days"
        for day in days("2026-10-24", "2026-11-22"):
            lake.land(day, 1440)
    elif data_set == "SIM02":
        assert name == "three-days"
        sim02(lake)
    elif data_set == "SIM04":
        assert name == "failed-copy"
        sim02(lake)
        lake.athena.ctas_rows["2026-11-21"] = 17268
    elif data_set == "SIM05":
        assert name == "late-file"
        sim02(lake)
        lake.night(utc("2026-11-23T11:05:00Z"))
        assert lake.location("2026-11-21").startswith(f"s3://{lake.bucket}/compacted/"), "the day was not moved"
        lake.marks["late"] = lake.land("2026-11-21", 1, start=1440, register=False)[0]
    else:
        raise AssertionError(data_set)


@given('the simulated data set SIM03 "change-log-day" is in trip_changes')
def step_change_log(context):
    lake = lake_of(context)
    lake.athena.files["recipe_ana_cdc.trip_changes"] = {"2026-11-22": 1440, "2026-11-21": 40}


@given('a 2-day copy of SIM03 "change-log-day" as table {table}')
def step_change_log_copy(context, table):
    """@live: an Iceberg table with the columns of trip_changes, 1,440 data files on 2026-11-22 and 40 on
    2026-11-21 (PyIceberg, one commit)."""
    assert getattr(context, "live", False), "this example runs with --tags=@live"
    lake = lake_of(context)
    lake.iceberg_copy(table, {"2026-11-22": 1440, "2026-11-21": 40})
    lake.marks["iceberg"] = table
    assert lake.iceberg_files(table) == {"2026-11-21": 40, "2026-11-22": 1440}, lake.iceberg_files(table)


@given("ICEBERG_TABLES also names table daily_totals, which does not have a day partition, and {n:d} data files")
def step_daily_totals(context, n):
    os.environ["ICEBERG_TABLES"] = ("recipe_ana_cdc.trip_changes:change_time,recipe_ana_lake.trips:requested_time,"
                                    "recipe_ana_lake.daily_totals:")
    lake_of(context).athena.files["recipe_ana_lake.daily_totals"] = {"whole table": n}


@given("day {day} of trip_events_parquet holds {n:d} files")
def step_day_holds(context, day, n):
    lake_of(context).land(day, n)


@given("days {first} to {last} of trip_events_parquet hold {n:d} files each")
def step_days_hold(context, first, last, n):
    for day in days(first, last):
        lake_of(context).land(day, n)


@given("trip_events_parquet is filed by {how} day, with CLOSE_AFTER_HOURS {hours:d}")
def step_filed_by(context, how, hours):
    os.environ["CLOSE_AFTER_HOURS"] = str(hours)


@given("QUERY_DAYS is {n:d}")
def step_query_days(context, n):
    os.environ["QUERY_DAYS"] = str(n)


@given("day {day} of {table} holds {n:d} data files and day {other} holds {m:d}")
def step_iceberg_two_days(context, day, table, n, other, m):
    db = "recipe_ana_cdc" if table == "trip_changes" else "recipe_ana_lake"
    lake_of(context).athena.files[f"{db}.{table}"] = {day: n, other: m}


@given("day {day} of {table} holds {n:d} data files")
def step_iceberg_day(context, day, table, n):
    db = "recipe_ana_cdc" if table == "trip_changes" else "recipe_ana_lake"
    lake_of(context).athena.files[f"{db}.{table}"] = {day: n}


@given("Athena refuses the WHERE of the OPTIMIZE of {table}")
def step_refuse(context, table):
    lake = lake_of(context)
    lake.athena.refuse.add(table)
    lake.athena.files["recipe_ana_lake.trips"] = {"2026-11-22": 720}       # a second table, which is compacted


@given("trip_events_parquet has the table property projection.enabled = true")
def step_projection_on(context):
    lake = lake_of(context)
    t = lake.glue.get_table(DatabaseName=lake.db, Name=lake.raw)["Table"]
    keep = ("Name", "StorageDescriptor", "PartitionKeys", "TableType")
    lake.glue.update_table(DatabaseName=lake.db, TableInput={**{k: t[k] for k in keep if k in t},
                                                             "Parameters": {**t.get("Parameters", {}),
                                                                            "projection.enabled": "true"}})
    # With projection on, the partitions are computed from the pattern: none is registered.
    parts = lake.glue.get_partitions(DatabaseName=lake.db, TableName=lake.raw)["Partitions"]
    if parts:
        lake.glue.batch_delete_partition(DatabaseName=lake.db, TableName=lake.raw,
                                         PartitionsToDelete=[{"Values": p["Values"]} for p in parts])
    lake.marks["partitions"] = []
    lake.marks["raw"] = lake.keys("raw/trip-events/")


@given("RAW_TABLE names {name}, which reads JSON Lines files")
def step_json_table(context, name):
    lake = lake_of(context)
    db, table = name.split(".")
    lake.glue.create_table(DatabaseName=db, TableInput={
        "Name": table, "PartitionKeys": [{"Name": "dt", "Type": "string"}],
        "StorageDescriptor": {"Columns": [{"Name": "event_id", "Type": "string"}],
                              "Location": f"s3://{lake.bucket}/raw/trip-events/",
                              "SerdeInfo": {"SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"}}})
    os.environ["RAW_TABLE"] = name
    lake.cd.register(lake.glue, db, table, lake.bucket, ["2026-11-22"])
    lake.marks["json"] = lake.glue.get_partitions(DatabaseName=db, TableName=table)["Partitions"]


@given("the CTAS of day {day} writes {n:d} rows")
def step_ctas_rows(context, day, n):
    lake_of(context).athena.ctas_rows[day] = n


@given("the count of dt={day} through trip_events_parquet after the switch is {n:d}")
def step_after_rows(context, day, n):
    lake_of(context).athena.after_rows[day] = n


@given("a 1441st file lands under raw/trip-events/dt={day}/ after the file list is recorded and before the switch")
def step_file_lands(context, day):
    lake = lake_of(context)
    lake.after_list = lambda d: d == day and lake.land(day, 1, start=1440, register=False)


@given("the partition dt={day} of trip_events_parquet was moved at {t}")
def step_moved(context, day, t):
    lake = lake_of(context)
    sim02(lake)
    lake.night(utc(t))
    assert lake.location(day).startswith(f"s3://{lake.bucket}/compacted/"), lake.location(day)


@given("MERGE_READS_RAW is 1, and the merge's manifest of dt={day} lists {n:d} of the day's {total:d} files as merged")
def step_manifest(context, day, n, total):
    os.environ["MERGE_READS_RAW"] = "1"
    lake = lake_of(context)
    listed = lake.keys(f"raw/trip-events/dt={day}/")
    assert len(listed) == total
    text = "".join(f"{k}\t{'merged' if i < n else 'quarantined'}\n" for i, k in enumerate(listed))
    lake.s3.put_object(Bucket=lake.bucket, Key=f"lake/merge-state/dt={day}.txt", Body=text.encode())


@given("MAX_MEASURE_USD is {usd}")
def step_max_measure(context, usd):
    os.environ["MAX_MEASURE_USD"] = usd


@given("the partitions of trip_events_parquet up to dt={day} are registered")
def step_registered_up_to(context, day):
    lake = lake_of(context)
    for d in days("2026-11-20", day):
        lake.land(d, 5)
    p = lake.glue.get_partition(DatabaseName=lake.db, TableName=lake.raw, PartitionValues=[day])["Partition"]
    lake.marks["kept"] = p["StorageDescriptor"]["Location"]


# ---- the night --------------------------------------------------------------------------------------------------

@when("the nightly schedule fires")
@when("the nightly run decides")
@when("the nightly run compacts {table}")
@when("the nightly run of 2026-11-23 ends")
def step_night(context, table=None):
    lake_of(context).night()


@when("the nightly run starts at {t}")
def step_night_at(context, t):
    lake_of(context).night(utc(t))


@when('{name} runs with "{args}" on {day}')
def step_run_on(context, name, args, day):
    import importlib

    import nightly_ops
    import support
    lake_of(context)
    nightly_ops.clock = lambda: utc(f"{day}T11:00:00Z")
    module = importlib.import_module(name.removesuffix(".py"))
    support.run(context, module.main, argv=args.split())


# ---- what the night did --------------------------------------------------------------------------------------

def last(context) -> dict:
    return lake_of(context).items[-1]


@then("the run log has {a:d} files for {da}, {b:d} for {db} and {c:d} for {dc} in trip_events_parquet")
def step_files_logged(context, a, da, b, db, c, dc):
    files = last(context)["files_by_day"]["recipe_ana_lake.trip_events_parquet"]
    assert (files[da], files[db], files[dc]) == (a, b, c), files


@then("the run log has {n:d} files for the whole of {table}")
def step_whole_logged(context, n, table):
    assert last(context)["files_by_day"][f"recipe_ana_lake.{table}"] == {"whole table": n}


@then("day {day} is {decision}")
def step_decision(context, day, decision):
    marked = last(context).get("marked", [])
    name = f"recipe_ana_lake.trip_events_parquet {day}"
    assert (name in marked) == (decision == "compacted"), marked


@then("the limit is {n:d} files a day")
def step_limit(context, n):
    assert last(context)["limit_per_day"] == n


@then("days {first} to {last_day} are compacted")
def step_days_compacted(context, first, last_day):
    marked = last(context)["marked"]
    assert marked == [f"recipe_ana_lake.trip_events_parquet {d}" for d in days(first, last_day)], marked


@then("days {a} and {b} wait for the next night")
def step_days_wait(context, a, b):
    waiting = last(context)["waiting"]
    assert waiting == [f"recipe_ana_lake.trip_events_parquet {d}" for d in (a, b)], waiting


@then("Athena was sent one OPTIMIZE of {table}, with WHERE {where}")
def step_one_optimize(context, table, where):
    sent = [s for s in lake_of(context).athena.statements("OPTIMIZE") if f".{table} " in s]
    assert len(sent) == 1 and sent[0].endswith(f"WHERE {where}"), sent


@then("Athena was sent no OPTIMIZE for {day}")
def step_no_optimize_day(context, day):
    assert not [s for s in lake_of(context).athena.statements("OPTIMIZE") if f"'{day} 00:00:00' AND" in s]


@then('Athena was sent the statement "{sql}"')
def step_sent(context, sql):
    assert sql in lake_of(context).athena.statements(), lake_of(context).athena.statements("OPTIMIZE")


@then("Athena was sent no OPTIMIZE of {table} without a WHERE")
def step_no_whole(context, table):
    sent = [s for s in lake_of(context).athena.statements("OPTIMIZE") if f".{table} " in s or s.endswith(f".{table}")]
    assert sent and all("WHERE" in s for s in sent), sent


@then('the run log has the run with failed_step "{step}" and the refusal as error_text')
def step_refusal_logged(context, step):
    item = last(context)
    assert item["failed_step"] == step and "Unexpected FilterNode" in item["error_text"], item
    trips = [s for s in lake_of(context).athena.statements("OPTIMIZE") if ".trips " in s]
    assert trips, "the other table was not compacted"


@then("the run ends with an error that names {table}")
def step_error_names(context, table):
    err = lake_of(context).error
    assert isinstance(err, RuntimeError) and table in str(err), err


@then('the run stops with the error "{text}"')
def step_stops(context, text):
    err = lake_of(context).error
    assert err is not None and str(err) == text, err
    assert lake_of(context).athena.sent == []


@then("no partition of trip_events_parquet changed and no file under raw/trip-events/ was deleted")
def step_nothing_changed(context):
    lake = lake_of(context)
    assert lake.glue.get_partitions(DatabaseName=lake.db, TableName=lake.raw)["Partitions"] == []
    assert lake.keys("raw/trip-events/") == lake.marks["raw"]


@then("no partition of trip_events changed")
def step_json_unchanged(context):
    lake = lake_of(context)
    now = lake.glue.get_partitions(DatabaseName="recipe_ana_lake", TableName="trip_events")["Partitions"]
    assert [p["StorageDescriptor"]["Location"] for p in now] == \
        [p["StorageDescriptor"]["Location"] for p in lake.marks["json"]]


@then("trip_events_parquet has the table property projection.enabled = false")
def step_projection_off(context):
    lake = lake_of(context)
    t = lake.glue.get_table(DatabaseName=lake.db, Name=lake.raw)["Table"]
    assert t["Parameters"]["projection.enabled"] == "false"


@then("the partitions dt={first} to dt={last_day} are registered, each at its raw/trip-events/ folder")
def step_registered(context, first, last_day):
    lake = lake_of(context)
    for d in days(first, last_day):
        assert lake.location(d) == f"s3://{lake.bucket}/raw/trip-events/dt={d}/", (d, lake.location(d))


@then("the partition dt={day} is unchanged")
def step_unchanged(context, day):
    lake = lake_of(context)
    assert lake.location(day) == lake.marks["kept"]


@then("maintenance/inputs/dt={day}/ holds the list of the day's {n:d} files")
def step_inputs(context, day, n):
    lake = lake_of(context)
    keys = lake.keys(f"maintenance/inputs/dt={day}/")
    assert len(keys) == 1
    listed = json.loads(lake.s3.get_object(Bucket=lake.bucket, Key=keys[0])["Body"].read())["files"]
    assert len(listed) == n


@then("the partition dt={day} of trip_events_parquet points at a prefix under {prefix}")
def step_points_under(context, day, prefix):
    lake = lake_of(context)
    assert lake.location(day).startswith(f"s3://{lake.bucket}/{prefix}"), lake.location(day)


@then("the partition dt={day} still points at {prefix}")
@then("the partition dt={day} points at {prefix} again")
@then("the partition dt={day} of trip_events_parquet still points at {prefix}")
def step_still_points(context, day, prefix):
    lake = lake_of(context)
    assert lake.location(day) == f"s3://{lake.bucket}/{prefix}", lake.location(day)


@then("the CTAS ran in the workgroup {wg}")
def step_ctas_wg(context, wg):
    assert [w for w, s in lake_of(context).athena.sent if s.startswith("CREATE TABLE")] == [wg]


@then("{prefix} is empty")
def step_empty(context, prefix):
    assert lake_of(context).keys(prefix) == []


@then('the run log has the failure of {day} with "{text}"')
def step_failure(context, day, text):
    item = last(context)
    assert item["failed_step"] == "copy" and f"{day}: {text}" in item["failed_days"], item


@then("raw/trip-events/dt={day}/ still holds its {n:d} files")
def step_raw_holds(context, day, n):
    assert len(lake_of(context).keys(f"raw/trip-events/dt={day}/")) == n


@then("the day's current location holds 1 file named late-<file name>")
def step_late_file(context):
    lake = lake_of(context)
    _, prefix = lake.cd.key_of(lake.location("2026-11-21"))
    late = [k for k in lake.keys(prefix) if k.rsplit("/", 1)[1].startswith("late-")]
    assert late == [f"{prefix}late-{lake.marks['late'].rsplit('/', 1)[1]}"], late


@then("the run log records {n:d} late file copied for {day}")
def step_late_logged(context, n, day):
    lake = lake_of(context)
    assert lake.cd.Log(lake.table).get(f"moved-{day}")["late_files_copied"] == n


@then("raw/trip-events/dt={day}/ holds the {n:d} listed files and not the late one")
def step_raw_listed(context, day, n):
    lake = lake_of(context)
    keys = lake.keys(f"raw/trip-events/dt={day}/")
    assert len(keys) == n and lake.marks["late"] not in keys


@then("Athena was sent no statement")
def step_no_statement(context):
    assert context.calls.queries == [] and lake_of(context).athena.sent == []


# ---- @live: what Athena answers after the night ----------------------------------------------------------

@then("the OPTIMIZE with the WHERE of {day} succeeds")
def step_optimize_ok(context, day):
    lake = lake_of(context)
    item = last(context)
    table = lake.marks["iceberg"]
    sent = [x for x in lake.athena.spent if x["sql"].startswith(f"OPTIMIZE {lake.db}.{table} ")]
    assert lake.error is None and item["run_state"] == "finished", (lake.error, item)
    assert [x["state"] for x in sent] == ["SUCCEEDED"] and f"{lake.db}.{table} {day}" in item["marked"], (sent, item)


@then("day {day} of {table} has fewer than {n:d} data files")
def step_iceberg_fewer(context, day, table, n):
    files = lake_of(context).iceberg_files(table)
    assert files[day] < n, files
    lake_of(context).printed.append(f"{table} data files per day after the night: {files}")


@then("day {day} of {table} still has {n:d} data files")
def step_iceberg_still(context, day, table, n):
    files = lake_of(context).iceberg_files(table)
    assert files[day] == n, files


@then("the count of trip_events_parquet for dt={day} is {n:d}, all of its rows read from compacted/trip-events/")
def step_count_compacted(context, day, n):
    lake = lake_of(context)
    row = lake.query(f"SELECT count(*) AS n, count_if(\"$path\" NOT LIKE 's3://{lake.bucket}/{lake.cd.COMPACTED}%') "
                     f"AS outside FROM {lake.db}.{lake.raw} WHERE dt = '{day}'")[0]
    lake.printed.append(f"count of dt={day}: {row['n']} rows, {row['outside']} outside compacted/")
    assert (int(row["n"]), int(row["outside"])) == (n, 0), row
    lake.marks["day"] = day


@then("the count of trip_events_parquet for dt={day} is {n:d}")
def step_count_day(context, day, n):
    lake = lake_of(context)
    row = lake.query(f"SELECT count(*) AS n FROM {lake.db}.{lake.raw} WHERE dt = '{day}'")[0]
    lake.printed.append(f"count of dt={day}: {row['n']} rows; the day's location {lake.location(day)}")
    assert int(row["n"]) == n, row


@then("that prefix holds fewer than {n:d} files")
def step_prefix_fewer(context, n):
    lake = lake_of(context)
    keys = lake.keys(f"{lake.cd.COMPACTED}dt={lake.marks['day']}/")
    lake.printed.append(f"files under {lake.cd.COMPACTED}dt={lake.marks['day']}/: {len(keys)}")
    assert 0 < len(keys) < n, keys


@then("before and after, the count by zone over 30 days is {a:d}, {b:d} and {c:d} for zones 48, 161 and 230")
def step_thirty_day_counts(context, a, b, c):
    rows = {}
    for line in context.printed.splitlines():
        for when in ("before", "after"):
            if line.startswith(f"count by zone {when}:"):
                rows[when] = {int(r["pickup_zone_id"]): int(r["trips"])
                              for r in json.loads(line.split(":", 1)[1])}
    assert rows.get("before") == rows.get("after") == {48: a, 161: b, 230: c}, (rows, context.printed[-2000:])


@then('the output has a row "before" with {n:d} files and a row "after" with fewer than {m:d}, each with Data '
      'scanned and run time')
def step_measure_rows(context, n, m):
    import re
    found = {}
    for line in context.printed.splitlines():
        hit = re.match(r"(before|after)\s+(\d+)\s+(\d+) B\s+(\d+) ms$", line)
        if hit:
            found[hit.group(1)] = [int(x) for x in hit.groups()[1:]]
    assert found["before"][0] == n and 0 < found["after"][0] < m, (found, context.printed[-2000:])
    assert all(v[1] > 0 and v[2] > 0 for v in found.values()), found


# ---- R113, R141: the deploy ------------------------------------------------------------------------------------

@then('the group "{group}" has the schedules "{a}" at "{cron_a}" and "{b}" at "{cron_b}", in the time zone "{zone}"')
def step_group_schedules(context, group, a, cron_a, b, cron_b, zone):
    import support
    sch = support.client("scheduler")
    got = {s["Name"]: sch.get_schedule(Name=s["Name"], GroupName=group)
           for s in sch.list_schedules(GroupName=group)["Schedules"]}
    assert sorted(got) == sorted([a, b]), sorted(got)
    assert got[a]["ScheduleExpression"] == cron_a and got[b]["ScheduleExpression"] == cron_b
    assert all(s["ScheduleExpressionTimezone"] == zone and s["State"] == "ENABLED" for s in got.values())
    tags = sch.list_tags_for_resource(ResourceArn=sch.get_schedule_group(Name=group)["Arn"])["Tags"]
    assert {"Key": "Project", "Value": "436c"} in tags


@then('each schedule calls the function "{function}" as the role "{role}", with no retries')
def step_schedule_function(context, function, role):
    for s in context.calls.of("CreateSchedule"):
        target = s["Target"]
        assert target["Arn"].endswith(f":function:{function}") and target["RoleArn"].endswith(f":role/{role}")
        assert target["RetryPolicy"]["MaximumRetryAttempts"] == 0
    import support
    lam = support.client("lambda")
    assert lam.get_function_event_invoke_config(FunctionName=function)["MaximumRetryAttempts"] == 0
    assert lam.get_function(FunctionName=function)["Configuration"]["Timeout"] == 900


@then("alarm {name} fires on {metric} of at least {n:d} over {period:d} s")
def step_errors_alarm(context, name, metric, n, period):
    import support
    a = support.client("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"][0]
    assert (a["MetricName"], a["Threshold"], a["ComparisonOperator"], a["Period"]) == \
        (metric, n, "GreaterThanOrEqualToThreshold", period), a


@then("alarm {name} fires on {metric} under {n:d} over {period:d} s, missing data breaching")
def step_missed_alarm(context, name, metric, n, period):
    import support
    a = support.client("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"][0]
    assert (a["MetricName"], a["Threshold"], a["ComparisonOperator"], a["Period"], a["TreatMissingData"]) == \
        (metric, n, "LessThanThreshold", period, "breaching"), a


@then("both send to topic {topic}, which has {email} subscribed")
def step_topic(context, topic, email):
    import support
    sns = support.client("sns")
    arn = next(t["TopicArn"] for t in sns.list_topics()["Topics"] if t["TopicArn"].endswith(f":{topic}"))
    subs = sns.list_subscriptions_by_topic(TopicArn=arn)["Subscriptions"]
    assert [(s["Protocol"], s["Endpoint"]) for s in subs] == [("email", email)]
    alarms = support.client("cloudwatch").describe_alarms(AlarmNamePrefix="recipe-ana-compact-")["MetricAlarms"]
    assert len(alarms) == 2 and all(a["AlarmActions"] == [arn] for a in alarms)
