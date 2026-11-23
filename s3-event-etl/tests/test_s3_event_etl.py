"""s3-event-etl offline: the check's rules, the start rules, the manifests and the notification entries (moto)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import check
import control
import etl_job

AT = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)


def run(minutes_ago, state, started=None):
    t = control.iso(AT - timedelta(minutes=minutes_ago))
    return {"run_id": {"S": t}, "run_state": {"S": state}, "started_at": {"S": started or t}}


@pytest.fixture
def glue(monkeypatch):
    monkeypatch.setenv("ENGINE", "glue")
    monkeypatch.delenv("ETL_MIN_GAP_MIN", raising=False)
    monkeypatch.delenv("MAX_RUNS_PER_DAY", raising=False)


def test_limits_differ_by_engine(monkeypatch, glue):
    assert control.limits() == (360, 4)
    monkeypatch.setenv("ENGINE", "athena")
    assert control.limits() == (60, 24)
    monkeypatch.setenv("ETL_MIN_GAP_MIN", "30")
    assert control.limits() == (30, 24)


def test_decide(glue):
    assert control.decide([], AT, {}) == (True, "start")
    assert control.decide([run(400, "running")], AT, {})[1] == "a run is running"
    assert "the sweep will start one" in control.decide([run(120, "finished")], AT, {})[1]
    four = [run(430, "finished"), run(420, "finished"), run(410, "finished"), run(400, "finished")]
    assert "until the next UTC day" in control.decide(four, AT, {})[1]
    failed = [run(1000, "failed"), run(800, "failed"), run(600, "failed")]
    assert control.decide(failed, AT, {})[1] == control.HELD
    assert control.decide(failed, AT, {"resumed_at": {"S": control.iso(AT)}}) == (True, "start")
    assert control.decide(failed + [run(500, "skipped")], AT, {})[1] == control.HELD   # a skip is not a run


def test_open_days_are_the_day_and_the_two_before():
    assert control.open_days(AT) == ["2026-11-21", "2026-11-22", "2026-11-23"]


def test_names_and_padded_sequencer():
    day, staged, quarantined = check.names("raw/trip-events/dt=2026-11-23/0905-run1.jsonl", "0A1B".zfill(32))
    assert day == "2026-11-23" and staged.endswith("/0905-run1-" + "0" * 28 + "0A1B.jsonl")
    assert quarantined.startswith("quarantine/trip-events-rows/dt=2026-11-23/")
    assert "000A1B".zfill(32) < "0A1B2C".zfill(32)          # the later write has the greater padded sequencer


def test_first_delivery_of_each_event_wins():
    rows = [{"event_id": "e1", "received_time": "2026-11-23T09:00:02Z", "k": 2},
            {"event_id": "e1", "received_time": "2026-11-23T09:00:01Z", "k": 1},
            {"event_id": "e2", "received_time": "2026-11-23T09:00:05Z", "k": 3}]
    assert sorted(r["k"] for r in etl_job.first_per_event(rows)) == [1, 3]


def test_merge_inserts_only_new_event_ids_on_named_days():
    sql = etl_job.merge_sql("recipe_lake", "staged", ["2026-11-23", "2026-11-22"])
    assert "ON t.event_id = s.event_id AND t.event_day IN (DATE '2026-11-22', DATE '2026-11-23')" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql and "WHEN MATCHED" not in sql


@pytest.fixture
def lake(aws, monkeypatch, tmp_path):
    import lake as lake_mod
    import run_log
    monkeypatch.setenv("PREFIX", "recipe")
    monkeypatch.setenv("START_ON_STAGE", "0")
    monkeypatch.setenv("ETL_LOCAL_DB", str(tmp_path / "lake.duckdb"))
    lake_mod.up(aws, log=lambda *a: None)
    run_log.ensure_run_log(aws)
    return aws, lake_mod.bucket_name(aws)


GOOD = {"delivery_id": "dl-1", "event_type": "trip_requested", "event_id": "ev-1", "trip_id": "t-1",
        "pickup_zone_id": 48, "event_time": "2026-11-23T09:05:10Z", "received_time": "2026-11-23T09:05:11Z"}


def test_a_later_sequencer_is_checked_again_and_an_earlier_one_is_a_repeat(lake):
    sess, bucket = lake
    s3 = sess.client("s3")
    key = "raw/trip-events/dt=2026-11-23/0905-a.jsonl"
    s3.put_object(Bucket=bucket, Key=key, Body=(json.dumps(GOOD) + "\n").encode())
    out = []
    assert check.check_object(bucket, key, "0A1B2C", sess, start=False, log=out.append)["good"] == 1
    assert check.check_object(bucket, key, "0A1B", sess, start=False, log=out.append)["repeat"]
    assert check.check_object(bucket, key, "0A1B2D", sess, start=False, log=out.append)["good"] == 1
    staged = etl_job._keys(s3, bucket, "stage/trip-events/")
    assert len(staged) == 2


def test_a_retry_of_the_same_event_after_a_crash_is_checked_not_counted(lake):
    sess, bucket = lake
    s3, ddb = sess.client("s3"), sess.client("dynamodb")
    key = "raw/trip-events/dt=2026-11-23/0905-b.jsonl"
    s3.put_object(Bucket=bucket, Key=key, Body=(json.dumps(GOOD) + "\n").encode())
    assert check.claim(ddb, key, "1".zfill(32))                       # the first try stopped after its PutItem
    assert check.check_object(bucket, key, "1", sess, start=False, log=lambda *a: None)["good"] == 1
    item = ddb.get_item(TableName="recipe-runs", Key={"job": {"S": "etl-object"}, "run_id": {"S": key}})["Item"]
    assert item["repeats"]["N"] == "0" and "checked_at" in item


def test_the_sweep_checks_an_object_whose_event_was_lost(lake):
    import sweep
    sess, bucket = lake
    s3 = sess.client("s3")
    key = "raw/trip-events/dt=2026-11-23/0905-lost.jsonl"
    s3.put_object(Bucket=bucket, Key=key, Body=(json.dumps(GOOD) + "\n").encode())
    assert sweep.lost_objects(sess, bucket, ["2026-11-23"], AT, log=lambda *a: None) == 1
    assert etl_job._keys(s3, bucket, "stage/trip-events/dt=2026-11-23/")


def test_the_local_load_twice_adds_nothing_and_the_manifest_lists_each_key(lake):
    import local_load
    sess, bucket = lake
    s3 = sess.client("s3")
    s3.put_object(Bucket=bucket, Key="stage/trip-events/dt=2026-11-23/a-1.jsonl", Body=(json.dumps(GOOD) + "\n").encode())
    assert local_load.load(s3, bucket, ["2026-11-23"])["inserted"] == 1
    s3.put_object(Bucket=bucket, Key="stage/trip-events/dt=2026-11-23/b-1.jsonl", Body=(json.dumps(GOOD) + "\n").encode())
    assert local_load.load(s3, bucket, ["2026-11-23"]) == {"objects": 1, "rows_in": 1, "inserted": 0}
    assert sorted(etl_job.manifest(s3, bucket, "2026-11-23")) == ["stage/trip-events/dt=2026-11-23/a-1.jsonl",
                                                                  "stage/trip-events/dt=2026-11-23/b-1.jsonl"]
    assert local_load.count() == 1


def test_a_failure_after_the_merge_leaves_the_manifest_and_names_its_step(lake):
    import local_load
    sess, bucket = lake
    s3 = sess.client("s3")
    s3.put_object(Bucket=bucket, Key="stage/trip-events/dt=2026-11-23/a-1.jsonl", Body=(json.dumps(GOOD) + "\n").encode())
    with pytest.raises(RuntimeError, match="^step=manifest:"):
        local_load.load(s3, bucket, ["2026-11-23"], fail_after_merge=True)
    assert etl_job.manifest(s3, bucket, "2026-11-23") == {}
    assert local_load.load(s3, bucket, ["2026-11-23"])["inserted"] == 0 and local_load.count() == 1


def test_the_notification_entry_is_added_and_removed_by_its_id(lake):
    import etl
    sess, bucket = lake
    s3 = sess.client("s3")
    sess.client("sqs").create_queue(QueueName="other")
    other = {"Id": "other", "QueueArn": "arn:aws:sqs:ca-central-1:123456789012:other", "Events": ["s3:ObjectCreated:*"]}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [other]})
    mine = {"Id": "recipe-s3-event-etl", "Events": ["s3:ObjectCreated:*"],
            "LambdaFunctionArn": "arn:aws:lambda:ca-central-1:123456789012:function:recipe-etl-check"}
    etl.add_entry(s3, bucket, mine)
    etl.add_entry(s3, bucket, mine)                              # replaced by its Id, not added twice
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    assert [e["Id"] for e in config["LambdaFunctionConfigurations"]] == ["recipe-s3-event-etl"]
    assert etl.remove_entry(s3, bucket, "recipe-s3-event-etl")
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    assert "LambdaFunctionConfigurations" not in config and config["QueueConfigurations"][0]["Id"] == "other"


def test_the_glue_job_script_compiles():
    import py_compile
    from pathlib import Path
    py_compile.compile(str(Path(etl_job.__file__)), doraise=True)


def test_the_athena_merge_reads_each_arrival_day_and_the_two_before():
    import athena_load
    todo = {"2026-11-23": ["stage/trip-events/dt=2026-11-23/a.jsonl"], "2026-11-21": ["k"]}
    assert athena_load.event_days_of(todo) == ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"]
    sql = etl_job.merge_sql("recipe_lake", "(SELECT 1) s0", athena_load.event_days_of(todo), catalog="awsdatacatalog")
    assert "t.event_day IN (DATE '2026-11-19', DATE '2026-11-20', DATE '2026-11-21', DATE '2026-11-22', " \
           "DATE '2026-11-23')" in sql
