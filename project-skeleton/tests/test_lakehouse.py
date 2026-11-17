import json

import lakehouse
from skeleton.config import names
from skeleton.events import distinct_in_window, generate
from skeleton.number import window


def seed_athena_result(value: str):
    """moto returns no rows for a query unless a result is queued first."""
    from moto.athena.models import QueryResults, athena_backends
    athena_backends["123456789012"]["ca-central-1"].query_results_queue.append(QueryResults(
        rows=[{"Data": [{"VarCharValue": "updates_last_hour"}]}, {"Data": [{"VarCharValue": value}]}],
        column_info=[{"Name": "updates_last_hour", "Type": "bigint"}]))


# ---------------------------------------------------------------- offline path ---------------------------------------

def test_local_number_matches_plain_python(local_root, as_of):
    events = generate(600, now=as_of, duplicates=5)
    start, end = window(as_of)
    n = lakehouse.run_local(events, local_root, start, end, arrived=as_of)
    assert n == distinct_in_window(events, start, end)
    assert 0 < n < 600                       # spread over two hours: about half fall in the last one


def test_copies_reach_the_lake_but_count_once(local_root, as_of):
    events = generate(100, now=as_of, duplicates=0)
    start, end = window(as_of)
    once = lakehouse.run_local(events, local_root, start, end, arrived=as_of)
    twice = lakehouse.run_local(events + events, local_root, start, end, arrived=as_of)
    assert once == twice


def test_files_land_under_the_arrival_day(local_root, as_of):
    start, end = window(as_of)
    lakehouse.run_local(generate(450, now=as_of), local_root, start, end, arrived=as_of, buffer_records=200)
    files = list(local_root.glob(f"updates/dt={as_of:%Y-%m-%d}/*.parquet"))
    assert len(files) == 3                   # 455 records, 200 per buffer


# ---------------------------------------------------------------- AWS path under moto --------------------------------

def test_setup_creates_the_pipeline(aws):
    n = names("t1", account="123456789012")
    out = lakehouse.setup(aws, n, "123456789012", sleep=lambda s: None)
    fh = aws.client("firehose").describe_delivery_stream(DeliveryStreamName="t1-to-lake")["DeliveryStreamDescription"]
    dest = fh["Destinations"][0]["ExtendedS3DestinationDescription"]
    assert fh["DeliveryStreamType"] == "KinesisStreamAsSource"
    assert dest["BufferingHints"] == {"SizeInMBs": 64, "IntervalInSeconds": 60}
    assert dest["Prefix"] == "updates/dt=!{timestamp:yyyy-MM-dd}/"
    conv = dest["DataFormatConversionConfiguration"]
    assert conv["Enabled"] and "ParquetSerDe" in conv["OutputFormatConfiguration"]["Serializer"]
    table = aws.client("glue").get_table(DatabaseName="t1_lake", Name="updates")["Table"]
    assert table["Parameters"]["projection.enabled"] == "true"
    assert [c["Name"] for c in table["StorageDescriptor"]["Columns"]] == ["event_id", "entity_id", "event_time", "value"]
    wg = aws.client("athena").get_work_group(WorkGroup="t1-wg")["WorkGroup"]["Configuration"]
    assert wg["BytesScannedCutoffPerQuery"] == 1_000_000_000
    assert out["stream_arn"].endswith(":stream/t1-updates")


def test_firehose_role_reaches_only_its_resources(aws):
    n = names("t2", account="123456789012")
    lakehouse.setup(aws, n, "123456789012", sleep=lambda s: None)
    doc = aws.client("iam").get_role_policy(RoleName="t2-firehose-role", PolicyName="least-privilege")["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    resources = [r for s in doc["Statement"] for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    assert "*" not in resources
    assert all("t2" in r or r.endswith(":catalog") for r in resources)


def test_send_puts_every_record_keyed_by_entity(aws, as_of):
    n = names("t3", account="123456789012")
    lakehouse.setup(aws, n, "123456789012", sleep=lambda s: None)
    events = generate(120, now=as_of, duplicates=0)
    kin = aws.client("kinesis")
    assert lakehouse.send(kin, "t3-updates", events) == 120
    shard = kin.list_shards(StreamName="t3-updates")["Shards"][0]["ShardId"]
    it = kin.get_shard_iterator(StreamName="t3-updates", ShardId=shard, ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    records = kin.get_records(ShardIterator=it, Limit=1000)["Records"]
    assert len(records) == 120
    first = json.loads(records[0]["Data"])
    assert records[0]["PartitionKey"] == first["entity_id"]


def test_number_from_athena(aws, as_of):
    n = names("t4", account="123456789012")
    lakehouse.setup(aws, n, "123456789012", sleep=lambda s: None)
    seed_athena_result("287")
    start, end = window(as_of)
    assert lakehouse.number_aws(aws, n, start, end)[0] == 287
