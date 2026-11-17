"""Main scenario, offline: moto stands in for Kinesis and DynamoDB, local_poller for Lambda's poller."""

import base64
import json

import boto3
import pytest
from moto import mock_aws

import consumer
import lag
import producer
import run as main_run
from updates import make_updates, stamp

REGION = "ca-central-1"


def kinesis_event(records):
    return {"Records": [{"eventID": f"shardId-000000000000:{seq}", "kinesis": {
        "sequenceNumber": seq, "partitionKey": "d-1",
        "data": base64.b64encode(json.dumps(r).encode() if isinstance(r, dict) else r).decode(),
        "approximateArrivalTimestamp": 1_800_000_000.5}} for seq, r in records]}


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "update_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "update_id", "AttributeType": "S"}])
        yield ddb


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert lag.percentile(values, 50) == 50
    assert lag.percentile(values, 95) == 95
    assert lag.percentile([7], 95) == 7


def test_consumer_stores_times_and_lag(table):
    u = stamp(make_updates(1)[0], sent_ms=1_800_000_000_000)
    out = consumer.handler(kinesis_event([("1", u)]), ddb=table, table="t", clock=lambda: 1_800_000_001.0)
    assert out == {"batchItemFailures": []}
    item = table.get_item(TableName="t", Key={"update_id": {"S": u["update_id"]}})["Item"]
    assert int(item["entered_ms"]["N"]) == 1_800_000_000_500
    assert int(item["arrival_ms"]["N"]) == 1_800_000_001_000
    assert int(item["lag_ms"]["N"]) == 1_800_000_001_000 - int(item["event_ms"]["N"])


def test_redelivered_record_keeps_first_arrival(table):
    u = stamp(make_updates(1)[0], sent_ms=1_800_000_000_000)
    consumer.handler(kinesis_event([("1", u)]), ddb=table, table="t", clock=lambda: 1_800_000_001.0)
    consumer.handler(kinesis_event([("1", u)]), ddb=table, table="t", clock=lambda: 1_800_000_009.0)
    item = table.get_item(TableName="t", Key={"update_id": {"S": u["update_id"]}})["Item"]
    assert int(item["arrival_ms"]["N"]) == 1_800_000_001_000


def test_bad_record_is_reported_and_batch_stops_there(table):
    a, c = (stamp(u, 1_800_000_000_000) for u in make_updates(2))
    out = consumer.handler(kinesis_event([("1", a), ("2", b"not json"), ("3", c)]), ddb=table, table="t")
    assert out == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert table.scan(TableName="t", Select="COUNT")["Count"] == 1     # record 3 waits for the retry


def test_producer_retries_only_failed_records():
    class Flaky:
        def __init__(self):
            self.calls = []

        def put_records(self, StreamName, Records):
            self.calls.append(len(Records))
            if len(self.calls) == 1:
                return {"FailedRecordCount": 1, "Records": [{}, {"ErrorCode": "ProvisionedThroughputExceededException"}, {}]}
            return {"FailedRecordCount": 0, "Records": [{} for _ in Records]}

    k = Flaky()
    assert producer.put_batch(k, "s", [stamp(u, 0) for u in make_updates(3)]) == 1
    assert k.calls == [3, 1]


def test_whole_scenario_local():
    out = main_run.run_local(300, "recipe-test")
    s = out["summary"]
    assert out["sent"]["sent"] == 300 and s["stored"] == 300
    assert out["polled"]["batches_given_up"] == 0
    whole = s["lag: event to stored"]
    assert whole["median_s"] < whole["p95_s"]              # offline phones make the tail
    assert whole["p95_s"] >= 30                            # 1 update in 10 waited 30 s or more on its phone
