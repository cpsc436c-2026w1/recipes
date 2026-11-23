"""ext-backfill offline: the statements, the ids, the DynamoDB load's retries and counts, and the clean-up."""
import csv
import hashlib
import importlib.util
import random
from datetime import date
from pathlib import Path

import duckdb
import pytest

import dynamo_load
import statements as sql

EXT = Path(__file__).resolve().parent.parent / "ext-backfill"
SIM04 = EXT / "sim" / "SIM04" / "history.csv"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(f"ext_{name}", EXT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = load_module("backfill")


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PREFIX", "recipe")
    return tmp_path


def history(root: Path) -> list[dict]:
    cur = duckdb.sql(f"SELECT * FROM read_parquet('{root}/.local/ext-backfill/lake/history/trips/*/*.parquet', "
                     f"hive_partitioning = true) ORDER BY trip_id")
    return [dict(zip(cur.columns, r)) for r in cur.fetchall()]


def test_months_split_a_range_by_calendar_month():
    days = backfill.days_between(date(2024, 1, 30), date(2024, 3, 2))
    assert [(m[0], m[-1]) for m in sql.months(days)] == [
        (date(2024, 1, 30), date(2024, 1, 31)), (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 2))]


def test_course_filter_reads_pickups_one_day_wider_and_lists_days_with_a_gap():
    src = sql.course_source()
    f = sql.source_filter(src, [date(2024, 3, 1), date(2024, 3, 30)], sql.ATHENA)
    assert "pickup_datetime < TIMESTAMP '2024-04-01 00:00:00'" in f      # 30 March + 2 days: March's partition only
    assert "IN (DATE" in f and "hvfhs_license_num = 'HV0003'" in f and "pulocationid IN (48, 161, 230)" in f


def test_athena_date_parse_codes():
    assert sql.athena_time_format("%Y-%m-%d %H:%M:%S") == "%Y-%m-%d %H:%i:%s"


def test_ctas_puts_the_partition_column_last_and_writes_zstd_parquet():
    text = sql.ctas(sql.course_source(), "course.course_rides.rides", "recipe_lake", "s3://b/history/trips/",
                    [date(2024, 3, 1)], "bf-1")
    assert "partitioned_by = ARRAY['requested_day']" in text and "write_compression = 'ZSTD'" in text
    assert text.split("FROM (", 1)[0].rstrip().endswith("requested_day")


def test_a_microsecond_timestamp_is_written_as_milliseconds():
    """The course table's request_datetime is timestamp(6); a Hive Parquet CTAS takes milliseconds only (refused
    live, 2026-11-23), so the Athena text casts it; the DuckDB twin keeps the column as it is."""
    src = sql.course_source()
    athena = sql.history_select(src, "course.course_rides.rides", [date(2024, 3, 1)], "bf-1", sql.ATHENA)
    duck = sql.history_select(src, "source_course", [date(2024, 3, 1)], "bf-1", sql.DUCKDB)
    assert "CAST(request_datetime AS timestamp) AS requested_time" in athena
    assert "request_datetime AS requested_time" in duck and "CAST(request_datetime AS timestamp)" not in duck


def test_ids_of_a_csv_source_are_the_python_hash_of_its_text(here):
    """For text columns the hashed text is the same in Athena and DuckDB; Python gives the same id."""
    (here / "raw").mkdir()
    (here / "raw" / "h.csv").write_text(SIM04.read_text())
    assert backfill.main(["--source", "files", "--files", "raw", "--from", "2024-03-14", "--to", "2024-03-15",
                          "--run-id", "bf-t"]) == 0
    with open(SIM04) as f:
        rows = list(csv.reader(f))[1:]
    expected, seen = [], {}
    for r in rows:
        h = hashlib.sha256("|".join(r).encode()).hexdigest()[:16]
        seen[h] = seen.get(h, 0) + 1
        expected.append(f"{h}-{seen[h]}")
    assert sorted(r["trip_id"] for r in history(here)) == sorted(expected)


def test_a_bad_number_in_a_csv_becomes_null_and_is_still_counted(here):
    (here / "raw").mkdir()
    text = SIM04.read_text().replace(",14.54,", ",n/a,", 1)
    (here / "raw" / "h.csv").write_text(text)
    assert backfill.main(["--source", "files", "--files", "raw", "--from", "2024-03-14", "--to", "2024-03-15"]) == 0
    fares = [r["fare"] for r in history(here)]
    assert len(fares) == 6 and fares.count(None) == 1


def test_dynamodb_numbers_and_nulls():
    item = dynamo_load.to_item({"trip_id": "h-1", "fare": 18.4, "trip_time_s": 483, "dropoff_zone_id": None,
                                "requested_day": date(2024, 3, 14)}, "ld-1")
    assert item == {"trip_id": {"S": "h-1"}, "fare": {"N": "18.4"}, "trip_time_s": {"N": "483"},
                    "requested_day": {"S": "2024-03-14"}, "load_id": {"S": "ld-1"}}
    assert dynamo_load.number(1e-05) == "0.00001" and dynamo_load.number(20.0) == "20"


class Throttled:
    """A DynamoDB client whose BatchWriteItem leaves the last `k` items of a call unprocessed, `times` times."""

    def __init__(self, k=5, times=2):
        self.k, self.times, self.calls, self.stored = k, times, [], {}

    def batch_write_item(self, RequestItems):
        (table, reqs), = RequestItems.items()
        self.calls.append(len(reqs))
        keep, left = (reqs[:-self.k], reqs[-self.k:]) if self.times else (reqs, [])
        self.times -= 1 if self.times > 0 else 0
        for r in keep:
            self.stored[r["PutRequest"]["Item"]["trip_id"]["S"]] = r
        return {"UnprocessedItems": {table: left} if left else {}}


def items(n):
    return [dynamo_load.to_item({"trip_id": f"h-{i:03d}-1"}, "ld") for i in range(n)]


def test_unprocessed_items_are_sent_again_after_waits_under_a_doubling_limit():
    waits, client = [], Throttled(times=2)
    calls = dynamo_load.batch_write(client, "t", items(60), sleep=waits.append, rng=random.Random(1))
    assert len(client.stored) == 60 and calls == 5 and client.calls == [25, 5, 5, 25, 10]
    assert len(waits) == 2 and waits[0] < 0.1 and waits[1] < 0.2


def test_items_left_after_five_attempts_stop_with_their_number_and_first_id():
    waits = []
    with pytest.raises(dynamo_load.Unprocessed, match="5 items unprocessed after 5 attempts; the first is "
                                                      "trip_id h-020-1"):
        dynamo_load.batch_write(Throttled(times=-1), "t", items(60), sleep=waits.append, rng=random.Random(1))
    assert [w < lim for w, lim in zip(waits, (0.1, 0.2, 0.4, 0.8))] == [True] * 4 and len(waits) == 4


def test_a_batch_with_a_repeated_key_is_refused_before_it_is_sent():
    with pytest.raises(ValueError, match="repeats a trip_id"):
        dynamo_load.batch_write(Throttled(times=0), "t", items(3) + items(1), sleep=lambda s: None)


class Pages:
    def __init__(self, counts):
        self.counts, self.asked = counts, []

    def scan(self, **kw):
        self.asked.append(kw)
        i = len(self.asked) - 1
        page = {"Count": self.counts[i]}
        if i + 1 < len(self.counts):
            page["LastEvaluatedKey"] = {"trip_id": {"S": f"k{i}"}}
        return page


def test_the_count_scan_follows_every_page_strongly_consistent():
    pages = Pages([10, 0, 7])
    assert dynamo_load.count_load(pages, "t", "ld-2") == 17
    assert all(a["ConsistentRead"] and a["Select"] == "COUNT" for a in pages.asked)
    assert "ExclusiveStartKey" not in pages.asked[0] and pages.asked[2]["ExclusiveStartKey"] == {"trip_id": {"S": "k1"}}


def test_a_workgroup_made_by_rep_lakehouse_first_is_borrowed(aws, here):
    """rep-lakehouse-first's ensure_workgroup gives <PREFIX>-wg the settings scripts/lake.py wants, so a newer recipe
    with the same PREFIX borrows it (before 2026-11-23 its 1,000,000,000-byte cutoff stopped ext-backfill)."""
    import lake
    import tags
    from skeleton.aws import ensure_workgroup as skeleton_workgroup

    lake.up(aws, log=lambda *a: None)
    skeleton_workgroup(aws.client("athena"), "recipe-wg", lake.bucket_name(aws))
    assert lake.ensure_workgroup("recipe-wg", sess=aws) == "borrowed"
    assert tags.recorded("athena-workgroup", "recipe-wg") == "borrowed"


def test_a_workgroup_with_other_settings_stops_the_run_with_a_reason(aws, monkeypatch, here):
    athena = aws.client("athena")
    athena.create_work_group(Name="recipe-wg", Configuration={"BytesScannedCutoffPerQuery": 1_000_000_000,
                                                               "EnforceWorkGroupConfiguration": True})
    with pytest.raises(SystemExit, match="is never changed: delete it, or use another PREFIX"):
        backfill.main(["--target", "aws", "--check-only", "--from", "2024-03-01", "--to", "2024-03-02"])


def test_clean_up_deletes_only_what_the_extension_owns(aws, here):
    import tags

    clean = load_module("clean_up")
    s3, glue, ddb = aws.client("s3"), aws.client("glue"), aws.client("dynamodb")
    bucket = "recipe-lake-123456789012"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    for key in ("history/trips/requested_day=2024-03-01/a.parquet", "imports/trips-by-id/ld/part-0.json.gz",
                "raw/history/history.csv", "raw/trip-events/dt=2026-11-23/x.jsonl"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    glue.create_database(DatabaseInput={"Name": "recipe_lake"})
    tags.record("glue-database", "recipe_lake", "created")
    for t in ("trips_history", "history_raw", "trips_all", "trips"):
        glue.create_table(DatabaseName="recipe_lake", TableInput={"Name": t})
    ddb.create_table(TableName="recipe-trips-by-id", BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "trip_id", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "trip_id", "KeyType": "HASH"}])
    lines = []
    assert clean.clean_aws(aws, yes=False, log=lines.append) == 0           # a dry run deletes nothing
    assert "recipe-trips-by-id" in ddb.list_tables()["TableNames"]
    assert clean.clean_aws(aws, yes=True, log=lines.append) == 0
    left = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"])
    assert left == ["raw/history/history.csv", "raw/trip-events/dt=2026-11-23/x.jsonl"]
    assert [t["Name"] for t in glue.get_tables(DatabaseName="recipe_lake")["TableList"]] == ["trips"]
    assert any("kept     Glue database recipe_lake: it still holds trips" in ln for ln in lines)
    assert "recipe-trips-by-id" not in ddb.list_tables()["TableNames"]
    again = []
    assert clean.clean_aws(aws, yes=True, log=again.append) == 0             # a second run finds everything absent
    assert not [ln for ln in again if ln.startswith("removed")]


def test_the_tables_athena_makes_are_in_the_ledger(aws, here):
    """A Glue table made by a CTAS or a CREATE VIEW takes no tags; the live teardown found trips_history by its name
    only, so the engine records it, and clean_up.py records its delete."""
    import tags
    from datetime import datetime as dt
    from engines import AthenaEngine

    aws.client("s3").create_bucket(Bucket="recipe-lake-b", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    aws.client("athena").create_work_group(Name="wg", Configuration={})
    engine = AthenaEngine("recipe_lake", "wg", "wg", aws, "recipe-lake-b")
    engine.copy(sql.course_source(), [date(2024, 3, 1)], "bf-1", first=True)
    engine.copy(sql.course_source(), [date(2024, 3, 2)], "bf-1", first=False)
    engine.make_view(dt(2026, 11, 23, 14, 0))
    assert tags.recorded("glue-table", "recipe_lake.trips_history") == "created"
    assert tags.recorded("glue-table", "recipe_lake.trips_all") == "created"
    assert [r["state"] for r in tags.ledger_rows() if r["name"] == "recipe_lake.trips_history"] == ["created"]
