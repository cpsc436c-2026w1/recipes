"""ext-maintenance's nightly compaction: the build tests of its spec, the deploy and the clean-up, offline (moto and
the recording stand-in for Athena, tests/athena_stub.py)."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path += [p for p in [str(ROOT / "ext-maintenance"), str(ROOT / "scripts"), str(ROOT / "tests"), str(ROOT)] if p not in sys.path]   # appended: the recipe's clean_up.py stays first

import compact_days as cd  # noqa: E402
import nightly_ops  # noqa: E402
from athena_stub import StubAthena  # noqa: E402

DAY = "2026-11-21"
MOVE = datetime(2026, 11, 23, 11, 5, tzinfo=timezone.utc)
TWO_DAYS_ON = datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc)


def load_clean_up():
    spec = importlib.util.spec_from_file_location("maintenance_clean_up", ROOT / "ext-maintenance" / "clean_up.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lake(aws, monkeypatch):
    import lake as shared_lake
    import run_log
    monkeypatch.setenv("PREFIX", "recipe-test")
    shared_lake.up(aws, log=lambda *_: None)
    run_log.ensure_run_log(aws)
    bucket = shared_lake.bucket_name(aws)
    s3, glue = aws.client("s3"), aws.client("glue")
    nightly_ops.ensure_raw_table(glue, cd.Config(bucket=bucket), bucket)

    class L:
        athena = StubAthena(s3, glue, bucket)
        table = aws.resource("dynamodb").Table("recipe-test-runs")

        def land(self, day, n, start=0, register=True):
            keys = [f"raw/trip-events/dt={day}/f-{i:05d}.parquet" for i in range(start, start + n)]
            for k in keys:
                s3.put_object(Bucket=bucket, Key=k, Body=b"PAR1")
            self.athena.day_rows[day] = self.athena.day_rows.get(day, 0) + 12 * n
            if register:
                cd.register(glue, "recipe_test_lake", "trip_events_parquet", bucket, [day])
            return keys

        def night(self, now):
            return cd.nightly(now, cd.Config(bucket=bucket), s3=s3, glue=glue, table=self.table, athena=self.athena,
                              log=lambda *_: None)

        def raw(self, day=DAY):
            return sorted(cd.listing(s3, bucket, f"raw/trip-events/dt={day}/"))

        def location(self, day=DAY):
            return cd.partition_location(glue, "recipe_test_lake", "trip_events_parquet", day)

        def manifest(self, states):
            body = "".join(f"{k}\t{v}\n" for k, v in states.items())
            s3.put_object(Bucket=bucket, Key=f"lake/merge-state/dt={DAY}.txt", Body=body.encode())

    L.s3, L.glue, L.bucket, L.sess = s3, glue, bucket, aws
    return L()


def test_quarantined_file_in_compacted_day(lake, monkeypatch):
    monkeypatch.setenv("MERGE_READS_RAW", "1")
    keys = lake.land(DAY, 1440)
    lake.manifest({k: "quarantined" if i == 700 else "merged" for i, k in enumerate(keys)})
    lake.night(MOVE)
    assert "compacted/" in lake.location()
    lake.night(TWO_DAYS_ON)
    assert lake.raw() == sorted(keys)
    moved = cd.Log(lake.table).get(f"moved-{DAY}")
    assert moved["raw_delete"] == "waiting" and "1 listed files not merged" in moved["raw_kept"]


def test_merge_never_finished(lake, monkeypatch):
    monkeypatch.setenv("MERGE_READS_RAW", "1")
    keys = lake.land(DAY, 1440)
    lake.night(MOVE)
    late = lake.land(DAY, 1, start=1440, register=False)
    lake.night(TWO_DAYS_ON)
    assert lake.raw() == sorted(keys + late)              # nothing copied, nothing deleted
    _, prefix = cd.key_of(lake.location())
    assert not [k for k in cd.listing(lake.s3, lake.bucket, prefix) if "late-" in k]
    assert cd.Log(lake.table).get(f"moved-{DAY}")["raw_kept"] == "no merge manifest of the day yet"


def test_partial_raw_delete_retry(lake, monkeypatch):
    monkeypatch.setattr(cd, "DELETE_BATCH", 500)          # 1,440 keys: three DeleteObjects calls
    lake.land(DAY, 1440)
    lake.night(MOVE)
    real, calls = lake.s3.delete_objects, []

    def flaky(**kw):
        calls.append(len(kw["Delete"]["Objects"]))
        if len(calls) == 2:
            raise RuntimeError("DeleteObjects failed (put in by the test)")
        return real(**kw)

    monkeypatch.setattr(lake.s3, "delete_objects", flaky)
    with pytest.raises(RuntimeError, match="put in by the test"):
        lake.night(TWO_DAYS_ON)
    assert cd.Log(lake.table).get(f"moved-{DAY}")["raw_delete"] == "started"
    assert len(lake.raw()) == 940                           # the first 500 went
    monkeypatch.setattr(lake.s3, "delete_objects", real)
    lake.table.delete_item(Key={"job": cd.JOB, "run_id": "lock"})   # the stopped run's lease
    lake.night(datetime(2026, 11, 26, 11, 0, tzinfo=timezone.utc))
    assert lake.raw() == [] and cd.Log(lake.table).get(f"moved-{DAY}")["raw_delete"] == "done"


def test_clean_up_between_the_two_runs_finishes_the_day_and_copies_it_back(lake, monkeypatch):
    monkeypatch.setattr(cd, "DELETE_BATCH", 500)
    lake.land(DAY, 1440)
    lake.night(MOVE)
    real, calls = lake.s3.delete_objects, []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("DeleteObjects failed (put in by the test)")
        return real(**kw)

    monkeypatch.setattr(lake.s3, "delete_objects", flaky)
    with pytest.raises(RuntimeError):
        lake.night(TWO_DAYS_ON)
    monkeypatch.setattr(lake.s3, "delete_objects", real)
    clean_up = load_clean_up()
    lines = []
    assert clean_up.clean(lake.sess, yes=True, athena=lake.athena, log=lines.append) == 0, lines
    assert lake.location() == f"s3://{lake.bucket}/raw/trip-events/dt={DAY}/"
    back = lake.raw()
    assert len(back) == 3 and all(k.rsplit("/", 1)[1].startswith("compacted-") for k in back)
    assert cd.listing(lake.s3, lake.bucket, "compacted/") == {}
    counts = [s for s in lake.athena.statements("SELECT count(*) AS n FROM recipe_test_lake.trip_events_parquet")]
    assert len(counts) >= 2                                 # counted before and after the partition moved back


def test_refused_optimize_raises_after_the_other_table(lake):
    lake.athena.files = {"recipe_test_cdc.trip_changes": {"2026-11-22": 1440},
                         "recipe_test_lake.trips": {"2026-11-22": 720}}
    lake.athena.refuse.add("trip_changes")
    with pytest.raises(RuntimeError, match="recipe_test_cdc.trip_changes"):
        lake.night(datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))
    sent = lake.athena.statements("OPTIMIZE")
    assert any("recipe_test_lake.trips " in s for s in sent) and all("WHERE" in s for s in sent)
    item = cd.Log(lake.table).get("2026-11-23T11:00:00.000Z")
    assert item["run_state"] == "failed" and item["failed_step"] == "optimize"


def test_a_missing_table_fails_the_night_with_an_error_after_the_other_tables(lake):
    # A count that fails was recorded and the run returned: Lambda's Errors metric did not count it, so the alarm
    # would not send (R141). It now raises after the other tables, like a refused OPTIMIZE.
    lake.athena.files = {"recipe_test_lake.trips": {"2026-11-22": 720}}
    lake.athena.missing.add("trip_changes")
    with pytest.raises(RuntimeError, match="recipe_test_cdc.trip_changes"):
        lake.night(datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))
    assert any("recipe_test_lake.trips " in s for s in lake.athena.statements("OPTIMIZE"))
    item = cd.Log(lake.table).get("2026-11-23T11:00:00.000Z")
    assert item["run_state"] == "failed" and "trip_changes" in item["error_text"]


def test_estimate_of_thirty_days_with_all_days():
    usd = nightly_ops.estimate(43200, [1440] * 30, 2 + 4 * 30)
    assert f"{usd:.2f}" == "0.08"


def test_optimize_and_files_statements():
    assert cd.optimize_sql("d", "t", "change_time", "2026-11-22") == (
        "OPTIMIZE d.t REWRITE DATA USING BIN_PACK WHERE change_time >= TIMESTAMP '2026-11-22 00:00:00' "
        "AND change_time < TIMESTAMP '2026-11-23 00:00:00'")
    assert cd.optimize_sql("d", "t", None, None) == "OPTIMIZE d.t REWRITE DATA USING BIN_PACK"
    assert 'FROM "d"."t$files"' in cd.files_sql("d", "t", None)
    assert cd.Config(prefix="recipe-x").iceberg_tables == [("recipe_x_cdc", "trip_changes", "change_time"),
                                                            ("recipe_x_lake", "trips", "requested_time")]
    assert cd.Config(query_days=365).limit == 8


def test_files_per_day_come_from_partitions_not_files():
    # Athena's "<table>$files" has no partition column (query 6055abcb-f53a-4d3c-a8d8-227cf71fc08f failed with
    # COLUMN_NOT_FOUND on partition.ts_day, 2026-11-23); "<table>$partitions" has partition.<column>_day as a date and
    # file_count, the data files of the day (query da226747-190e-42e3-aba6-a2e56b139406). output/athena-probes.txt
    sql = cd.files_sql("d", "t", "change_time")
    assert sql == ('SELECT CAST(partition.change_time_day AS varchar) AS day, file_count AS files '
                   'FROM "d"."t$partitions" ORDER BY 1')
    assert "$files" not in sql


def test_register_partitions_before_the_deploy_makes_the_raw_table(aws, monkeypatch):
    # The README runs --register-partitions before --schedule: the raw table may not exist yet.
    import lake as shared_lake
    monkeypatch.setenv("PREFIX", "recipe-test")
    shared_lake.up(aws, log=lambda *_: None)
    bucket = shared_lake.bucket_name(aws)
    aws.client("s3").put_object(Bucket=bucket, Key="raw/trip-events/dt=2026-11-21/a.parquet", Body=b"PAR1")
    added = nightly_ops.register_partitions(datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc), sess=aws,
                                            log=lambda *_: None)
    assert added == 8                                     # the day folder and the next 7 days
    t = aws.client("glue").get_table(DatabaseName="recipe_test_lake", Name="trip_events_parquet")["Table"]
    assert t["Parameters"]["projection.enabled"] == "false"


def test_deploy_then_clean_up_leaves_nothing(aws, monkeypatch):
    monkeypatch.setenv("PREFIX", "recipe-test")
    out = nightly_ops.deploy("oncall@example.com", aws, log=lambda *_: None, sleep=lambda s: None)
    lam = aws.client("lambda")
    cfg = lam.get_function(FunctionName="recipe-test-compact-days")["Configuration"]
    assert (cfg["Timeout"], cfg["Handler"]) == (900, "compact_days.handler")
    assert cfg["Environment"]["Variables"]["MERGE_READS_RAW"] == "0"
    clean_up = load_clean_up()
    lines = []
    assert clean_up.clean(aws, yes=True, log=lines.append) == 0, lines
    assert clean_up.check(aws, log=lines.append) == 0, lines
    assert out["topic_arn"] not in [t["TopicArn"] for t in aws.client("sns").list_topics()["Topics"]]
    assert aws.client("glue").get_table(DatabaseName="recipe_test_lake", Name="trip_events_parquet")   # kept


def test_deploy_sets_merge_reads_raw_when_the_merge_is_there(aws, monkeypatch):
    monkeypatch.setenv("PREFIX", "recipe-test")
    aws.client("scheduler").create_schedule_group(Name="recipe-test-scheduled-merge")
    nightly_ops.deploy(None, aws, log=lambda *_: None, sleep=lambda s: None)
    env = aws.client("lambda").get_function(FunctionName="recipe-test-compact-days")["Configuration"]["Environment"]
    assert env["Variables"]["MERGE_READS_RAW"] == "1"


def test_land_parquet_writes_one_file_a_slot(tmp_path):
    import land_parquet
    assert land_parquet.main(["--days", "1", "--files-per-day", "1440", "--out", str(tmp_path)]) == 0
    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1440
    import pyarrow.parquet as pq
    total = sum(pq.read_metadata(f).num_rows for f in files)
    assert total == 2515                                     # SIM01's deliveries, one day
    assert json.dumps(pq.read_schema(files[0]).names).count("event_id") == 1


def test_a_nightly_run_after_the_clean_up_finds_no_old_moved_day(lake):
    lake.land(DAY, 1440)
    lake.night(MOVE)
    clean_up = load_clean_up()
    lines = []
    assert clean_up.clean(lake.sess, yes=True, athena=lake.athena, log=lines.append) == 0, lines
    assert cd.Log(lake.table).moved_days() == []
    lake.night(TWO_DAYS_ON)                                   # a redeploy's first night: no NoSuchKey
    assert lake.location() == f"s3://{lake.bucket}/raw/trip-events/dt={DAY}/" or "compacted/" in lake.location()
