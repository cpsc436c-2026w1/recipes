"""ext-scheduled-merge: the build tests of its spec, the SQL texts, the deploy and the clean-up, offline (moto,
DuckDB)."""
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path += [p for p in [str(ROOT / "ext-scheduled-merge"), str(ROOT / "scripts"), str(ROOT / "sim")] if p not in sys.path]   # appended: the recipe's clean_up.py stays first

import merge  # noqa: E402
import merge_run as m  # noqa: E402

DAY = "2026-11-23"


def load_clean_up():
    """This extension's clean_up.py, under a name of its own (the recipe's clean_up.py is another module)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("merge_clean_up", ROOT / "ext-scheduled-merge" / "clean_up.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def req(event_id, trip, t, zone=161):
    return {"event_type": "trip_requested", "event_id": event_id, "trip_id": trip, "pickup_zone_id": zone,
            "event_time": t, "delivery_id": f"dl-{event_id}", "received_time": t}


@pytest.fixture
def lake(aws, monkeypatch):
    import lake as shared_lake
    import run_log
    monkeypatch.setenv("PREFIX", "recipe-test")
    shared_lake.up(aws, log=lambda *_: None)
    run_log.ensure_run_log(aws)

    class L:
        s3 = aws.client("s3")
        bucket = shared_lake.bucket_name(aws)
        store = m.Store(s3, bucket)
        table = aws.resource("dynamodb").Table("recipe-test-runs")
        engine = m.DuckEngine()
        n = 0

        def put(self, lines, stamp, day=DAY, raw=None):
            L.n += 1
            key = f"raw/trip-events/dt={day}/obj-{L.n:05d}.jsonl"
            body = raw if raw is not None else "".join(json.dumps(d) + "\n" for d in lines)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode())
            self.store.stamps[key] = stamp
            return key

        def run(self, t):
            return m.run(utc(t), store=self.store, table=self.table, engine=self.engine, log=lambda *_: None)

    return L()


def test_object_visible_two_days_late(lake):
    lake.put([req("e-1", "t-1", "2026-11-25T08:00:00.000Z")], utc("2026-11-25T08:01:00Z"), day="2026-11-25")
    assert lake.run("2026-11-25T09:00:00Z")["objects"] == 1
    late = lake.put([req("e-2", "t-2", "2026-11-23T08:00:00.000Z")], utc("2026-11-23T08:01:00Z"), day="2026-11-23")
    old = lake.put([req("e-3", "t-3", "2026-11-20T08:00:00.000Z")], utc("2026-11-20T08:01:00Z"), day="2026-11-20")
    item = lake.run("2026-11-25T10:00:00Z")
    assert item["taken"] == [late]
    assert m.manifest(lake.store, "2026-11-23") == {late: "merged"}
    assert old not in m.manifest(lake.store, "2026-11-20")


def test_quarantine_marked_in_manifest(lake):
    keys = []
    for i in range(60):
        line = req(f"e-{i}", f"t-{i}", "2026-11-23T09:00:00.000Z")
        if i == 30:
            del line["trip_id"]
        keys.append(lake.put([line], utc("2026-11-23T09:10:00Z") + timedelta(seconds=i)))
    item = lake.run("2026-11-23T10:00:00Z")
    states = m.manifest(lake.store, DAY)
    assert states[keys[30]] == "quarantined"
    assert sum(1 for v in states.values() if v == "merged") == 59
    trips = {r[0] for r in lake.engine.rows("SELECT trip_id FROM trips")}
    assert "t-30" not in trips and len(trips) == 59
    assert (item["quarantined"], item["bad_lines"]) == (1, 1)


@pytest.mark.parametrize("order", [("e-0701", "e-0702"), ("e-0702", "e-0701")])
def test_equal_time_correction(lake, order):
    zone = {"e-0701": 48, "e-0702": 161}
    lake.put([req(order[0], "t-0700", "2026-11-23T09:05:00.000Z", zone[order[0]])], utc("2026-11-23T09:10:00Z"))
    lake.run("2026-11-23T10:00:00Z")
    state = lake.engine.rows("SELECT trip_state FROM trips")
    lake.put([req(order[1], "t-0700", "2026-11-23T09:05:00.000Z", zone[order[1]])], utc("2026-11-23T10:10:00Z"))
    lake.run("2026-11-23T11:00:00Z")
    row = lake.engine.rows("SELECT pickup_zone_id, request_event_id, trip_state FROM trips")
    assert row == [(161, "e-0702", state[0][0])]


def test_the_source_has_one_row_per_trip():
    import sim_data
    deliveries, truth = sim_data.load("SIM03")
    obj = m.Obj("k", DAY, utc("2026-11-23T10:00:00Z"), lines=deliveries)
    engine = m.DuckEngine()
    source = m.sql_text("trips_source").format(events=engine.events([obj], None))
    rows = engine.rows(f"SELECT trip_id, count(*) FROM ({source}) GROUP BY 1 HAVING count(*) > 1")
    assert rows == []
    assert engine.rows(f"SELECT count(*) FROM ({source})") == [(truth["trips"],)]


def test_the_source_reads_the_run_s_events_once():
    # Athena does not keep a WITH result: each reference to the events read the objects again (the live MERGE of
    # 2026-11-23 scanned 4 x 695,906 B with four references; output/ext-scheduled-merge.txt).
    assert m.sql_text("trips_source").count("{events}") == 1
    assert m.sql_text("trips_source").count("FROM once") == 1


def test_athena_reads_only_the_run_s_objects_and_days():
    cfg = m.Config(prefix="recipe-test")
    engine = m.AthenaEngine(None, cfg)
    store = m.Store(None, "recipe-test-lake-111122223333")
    objs = [m.Obj(f"raw/trip-events/dt={d}/a.jsonl", d, utc("2026-11-23T09:00:00Z")) for d in (DAY, "2026-11-22")]
    sql = m.merge_sql(engine, objs, store)
    assert sql.count("MERGE INTO recipe_test_lake.trips t") == 1
    assert "WHERE dt IN ('2026-11-22', '2026-11-23')" in sql
    assert "'s3://recipe-test-lake-111122223333/raw/trip-events/dt=2026-11-23/a.jsonl'" in sql
    assert "--" not in sql                              # comment lines are taken out before the text is sent


def test_rebuild_ranges():
    run_day = datetime(2026, 11, 23).date()
    assert [str(d) for d in m.rebuild_range(run_day, m.Config(period="day"))] == ["2026-11-19", "2026-11-24"]
    assert [str(d) for d in m.rebuild_range(run_day, m.Config(period="week"))] == ["2026-11-16", "2026-11-30"]
    assert [str(d) for d in m.rebuild_range(run_day, m.Config(period="month"))] == ["2026-11-01", "2026-12-01"]
    with pytest.raises(ValueError):
        m.Config(period="year")


def test_ddl_has_the_database_and_three_tables():
    sql = merge.ddl_statements("recipe_test_lake", "recipe-test-lake-111122223333")
    assert len(sql) == 4 and sql[0] == "CREATE DATABASE IF NOT EXISTS recipe_test_lake"
    assert "'storage.location.template' = 's3://recipe-test-lake-111122223333/raw/trip-events/dt=${dt}/'" in sql[1]
    assert "PARTITIONED BY (day(requested_time))" in sql[2]


def test_the_package_holds_the_handler_its_sql_and_the_models():
    names = set(zipfile.ZipFile(io.BytesIO(merge.package(deps=False))).namelist())
    assert {"merge_run.py", "rides/events.py", "sql/merge_trips.sql", "sql/trips_source.sql",
            "sql/rebuild_counts.sql"} <= names


def test_up_then_clean_up_leaves_nothing(aws, monkeypatch):
    clean_up = load_clean_up()
    monkeypatch.setenv("PREFIX", "recipe-test")
    out = merge.up(aws, deps=False, log=lambda *_: None, sleep=lambda s: None)
    lam, sch = aws.client("lambda"), aws.client("scheduler")
    cfg = lam.get_function(FunctionName="recipe-test-merge-run")["Configuration"]
    assert (cfg["Timeout"], cfg["MemorySize"], cfg["Architectures"]) == (600, 256, ["arm64"])
    assert lam.get_function_event_invoke_config(FunctionName="recipe-test-merge-run")["MaximumRetryAttempts"] == 0
    s = sch.get_schedule(Name="recipe-test-merge-hourly", GroupName="recipe-test-scheduled-merge")
    assert s["ScheduleExpression"] == "rate(1 hour)" and s["Target"]["RetryPolicy"]["MaximumRetryAttempts"] == 0
    assert json.loads(s["Target"]["Input"]) == {"time": "<aws.scheduler.scheduled-time>"}
    tags = sch.list_tags_for_resource(ResourceArn=sch.get_schedule_group(Name="recipe-test-scheduled-merge")["Arn"])
    assert {"Key": "Project", "Value": "436c"} in tags["Tags"]
    m.RunLog(aws.resource("dynamodb").Table("recipe-test-runs")).put("2026-11-23T10:00:00.000Z", run_state="finished")
    lines = []
    assert clean_up.clean(aws, yes=False, log=lines.append) == 0           # dry run: nothing goes
    assert lam.get_function(FunctionName="recipe-test-merge-run")
    assert clean_up.clean(aws, yes=True, log=lines.append) == 0
    assert clean_up.check(aws, log=lines.append) == 0, lines
    assert [f for f in lam.list_functions()["Functions"]] == []
    assert out["bucket"] in [b["Name"] for b in aws.client("s3").list_buckets()["Buckets"]]   # the bucket stays


def test_clean_up_keeps_a_run_log_another_job_uses(aws, monkeypatch):
    clean_up = load_clean_up()
    import run_log
    monkeypatch.setenv("PREFIX", "recipe-test")
    import lake as shared_lake
    shared_lake.up(aws, log=lambda *_: None)
    run_log.ensure_run_log(aws)
    table = aws.resource("dynamodb").Table("recipe-test-runs")
    m.RunLog(table).put("2026-11-23T10:00:00.000Z", run_state="finished")
    m.RunLog(table, job="compact-days").put("2026-11-23T11:00:00.000Z", run_state="finished")
    lines = []
    clean_up.clean(aws, yes=True, log=lines.append)
    assert any("kept" in line and "compact-days" in line for line in lines), lines
    assert [i["job"] for i in table.scan()["Items"]] == ["compact-days"]


def test_clean_up_keeps_a_table_that_was_there_before_up(aws, monkeypatch):
    """Glue tables carry no tags: a table `up` found already there is `borrowed` and survives --yes, with its data
    prefix and its database; a table `up` created is deleted."""
    clean_up = load_clean_up()
    monkeypatch.setenv("PREFIX", "recipe-test")
    glue, s3 = aws.client("glue"), aws.client("s3")
    glue.create_database(DatabaseInput={"Name": "recipe_test_lake"})
    glue.create_table(DatabaseName="recipe_test_lake", TableInput={"Name": "trips"})
    out = merge.up(aws, deps=False, log=lambda *_: None, sleep=lambda s: None)
    glue.create_table(DatabaseName="recipe_test_lake", TableInput={"Name": "zone_counts"})   # as Athena DDL does
    s3.put_object(Bucket=out["bucket"], Key="lake/trips/data/a.parquet", Body=b"x")
    s3.put_object(Bucket=out["bucket"], Key="lake/zone_counts/data/b.parquet", Body=b"x")
    lines = []
    clean_up.clean(aws, yes=False, log=lines.append)
    assert any(l.startswith("kept") and "recipe_test_lake.trips (borrowed)" in l for l in lines), lines
    clean_up.clean(aws, yes=True, log=lines.append)
    tables = [t["Name"] for t in glue.get_tables(DatabaseName="recipe_test_lake")["TableList"]]
    assert tables == ["trips"]
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=out["bucket"], Prefix="lake/").get("Contents", [])]
    assert keys == ["lake/trips/data/a.parquet"]
    assert clean_up.check(aws, log=lines.append) == 0, lines      # what is left is not ours


def test_pause_and_resume_change_only_the_schedule_state(aws, monkeypatch):
    monkeypatch.setenv("PREFIX", "recipe-test")
    merge.up(aws, deps=False, log=lambda *_: None, sleep=lambda s: None)
    sch = aws.client("scheduler")
    before = sch.get_schedule(Name="recipe-test-merge-hourly", GroupName="recipe-test-scheduled-merge")
    merge.set_state("DISABLED", aws, log=lambda *_: None)
    paused = sch.get_schedule(Name="recipe-test-merge-hourly", GroupName="recipe-test-scheduled-merge")
    assert paused["State"] == "DISABLED"
    assert (paused["ScheduleExpression"], paused["Target"]) == (before["ScheduleExpression"], before["Target"])
    merge.set_state("ENABLED", aws, log=lambda *_: None)
    assert sch.get_schedule(Name="recipe-test-merge-hourly", GroupName="recipe-test-scheduled-merge")["State"] == \
        "ENABLED"
    assert aws.client("lambda").get_function(FunctionName="recipe-test-merge-run")      # nothing else touched


def test_the_offline_run_it_command_runs(capsys):
    assert merge.main([]) == 0
    assert "the same objects merged again leave trips unchanged: True" in capsys.readouterr().out


def test_a_run_returns_its_query_ids(lake):
    lake.put([req("e-1", "t-1", "2026-11-23T09:00:00.000Z")], utc("2026-11-23T09:10:00Z"))
    item = lake.run("2026-11-23T10:00:00Z")
    assert item["merge_query_id"].startswith("duck-") and item["rebuild_query_id"].startswith("duck-")
