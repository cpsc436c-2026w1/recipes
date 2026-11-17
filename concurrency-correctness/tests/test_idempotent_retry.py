"""A write that times out after it succeeded is retried with the same request id and applies once."""

import importlib.util
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws

import tips
import trips

REGION = "ca-central-1"
TABLE, REQUESTS = "recipe-test-race-trips", "recipe-test-race-requests"


@pytest.fixture
def ddb():
    with mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        trips.create_table(c, TABLE)
        tips.create_requests_table(c, REQUESTS)
        trips.reset_trip(c, TABLE, "t-1")
        yield c


def quiet(_):
    pass


def test_reply_lost_once_applies_then_raises(ddb):
    flaky = tips.ReplyLostOnce(ddb)
    with pytest.raises(ReadTimeoutError):
        tips.add_tip_naive(flaky, TABLE, "t-1", 5)
    assert trips.read_trip(ddb, TABLE, "t-1")["tip_total"] == 5          # applied although the app saw a timeout


def test_naive_retry_adds_the_tip_twice(ddb):
    flaky = tips.ReplyLostOnce(ddb)
    tips.send_with_retries(lambda: tips.add_tip_naive(flaky, TABLE, "t-1", 5), backoff_s=0, log=quiet)
    assert trips.read_trip(ddb, TABLE, "t-1")["tip_total"] == 10


def test_retry_with_request_id_adds_it_once(ddb):
    flaky = tips.ReplyLostOnce(ddb)
    rid = tips.new_request_id()
    result = tips.send_with_retries(lambda: tips.add_tip_once(flaky, TABLE, "t-1", 5, rid, REQUESTS),
                                    backoff_s=0, log=quiet)
    assert result == "already applied"
    assert trips.read_trip(ddb, TABLE, "t-1")["tip_total"] == 5
    marker = ddb.get_item(TableName=REQUESTS, Key={"request_id": {"S": rid}})["Item"]
    assert marker["trip_id"]["S"] == "t-1" and int(marker["expires_at"]["N"]) > 0


def test_two_taps_are_two_requests(ddb):
    for _ in range(2):
        assert tips.add_tip_once(ddb, TABLE, "t-1", 5, tips.new_request_id(), REQUESTS) == "applied"
    assert trips.read_trip(ddb, TABLE, "t-1")["tip_total"] == 10


def test_tip_on_missing_trip_is_refused_and_writes_no_marker(ddb):
    with pytest.raises(ddb.exceptions.TransactionCanceledException):
        tips.add_tip_once(ddb, TABLE, "t-none", 5, "r-1", REQUESTS)
    assert "Item" not in ddb.get_item(TableName=REQUESTS, Key={"request_id": {"S": "r-1"}})


def test_retries_are_limited():
    def always_times_out():
        raise ReadTimeoutError(endpoint_url="x")
    with pytest.raises(RuntimeError):
        tips.send_with_retries(always_times_out, attempts=3, backoff_s=0, log=quiet)


def test_extension_run_local():
    path = Path(__file__).parent.parent / "ext-idempotent-retry" / "run.py"
    spec = importlib.util.spec_from_file_location("retry_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--target", "local"]) == {"naive": 10, "request id": 5}
