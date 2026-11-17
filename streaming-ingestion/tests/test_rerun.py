"""A run right after a clean-up, and a second run over what --keep left. Offline: moto, with the answers moto does
not give (a resource still being deleted, a refused call) put in by hand."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import infra

PREFIX = "recipe-test"
REGION = "ca-central-1"


def load(name, rel):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def refuse(client, operation, code, times=1):
    """Make the next `times` calls of client.<operation> fail with this error code, then behave as before."""
    real, left = getattr(client, operation), [times]

    def call(**kw):
        if left[0] > 0:
            left[0] -= 1
            raise client.exceptions.from_code(code)({"Error": {"Code": code, "Message": "put in by the test"}}, operation)
        return real(**kw)
    setattr(client, operation, call)


def inventory(s) -> dict:
    return {
        "functions": [f["FunctionName"] for f in s.client("lambda").list_functions()["Functions"]],
        "mappings": [m["UUID"] for m in s.client("lambda").list_event_source_mappings()["EventSourceMappings"]],
        "tables": s.client("dynamodb").list_tables()["TableNames"],
        "streams": s.client("kinesis").list_streams()["StreamNames"],
        "queues": s.client("sqs").list_queues().get("QueueUrls", []),
        "topics": s.client("sns").list_topics()["Topics"],
        "subscriptions": s.client("sns").list_subscriptions()["Subscriptions"],
        "roles": [r["RoleName"] for r in s.client("iam").list_roles()["Roles"]],
        "log groups": [g["logGroupName"] for g in s.client("logs").describe_log_groups()["logGroups"]],
    }


def deploy_all(s):
    made = infra.deploy_main(s, PREFIX)
    load("windowed_run", "ext-windowed-aggregates/run.py").deploy(s, PREFIX, "drop", 120, 25)
    load("fanout_run", "ext-fan-out/run.py").make_topic_and_queues(s, PREFIX)
    return made


# ---------- a run right after a clean-up

@mock_aws
def test_a_stream_that_is_still_being_deleted_is_waited_out_and_created_again():
    kinesis = boto3.client("kinesis", region_name=REGION)
    refuse(kinesis, "create_stream", "ResourceInUseException")
    real_summary, real_waiter, waited = kinesis.describe_stream_summary, kinesis.get_waiter, []
    kinesis.describe_stream_summary = lambda **kw: (
        real_summary(**kw) if waited else {"StreamDescriptionSummary": {"StreamStatus": "DELETING"}})

    class Gone:
        def wait(self, **kw):
            waited.append(kw["StreamName"])
    kinesis.get_waiter = lambda name: Gone() if name == "stream_not_exists" else real_waiter(name)

    arn = infra.create_stream(kinesis, "recipe-updates", poll_s=0)
    assert waited == ["recipe-updates"]                                  # waited for the deletion to finish
    assert arn.endswith("stream/recipe-updates")
    assert kinesis.list_streams()["StreamNames"] == ["recipe-updates"]   # and a new stream was created


@mock_aws
def test_an_existing_stream_is_used_as_it_is():
    kinesis = boto3.client("kinesis", region_name=REGION)
    first = infra.create_stream(kinesis, "recipe-updates", poll_s=0)
    assert infra.create_stream(kinesis, "recipe-updates", poll_s=0) == first


@mock_aws
def test_a_table_that_is_still_being_deleted_is_waited_out_and_created_again():
    ddb = boto3.client("dynamodb", region_name=REGION)
    refuse(ddb, "create_table", "ResourceInUseException")
    real_describe, real_waiter, waited = ddb.describe_table, ddb.get_waiter, []
    ddb.describe_table = lambda **kw: real_describe(**kw) if waited else {"Table": {"TableStatus": "DELETING"}}

    class Gone:
        def wait(self, **kw):
            waited.append(kw["TableName"])
    ddb.get_waiter = lambda name: Gone() if name == "table_not_exists" else real_waiter(name)

    arn = infra.create_table(ddb, "recipe-updates", "update_id", poll_s=0)
    assert waited == ["recipe-updates"] and arn.endswith("table/recipe-updates")
    assert ddb.list_tables()["TableNames"] == ["recipe-updates"]


@mock_aws
def test_a_queue_name_deleted_a_moment_ago_is_tried_again():
    sqs = boto3.client("sqs", region_name=REGION)
    refuse(sqs, "create_queue", "AWS.SimpleQueueService.QueueDeletedRecently", times=2)
    _, arn = infra.create_queue(sqs, "recipe-consumer-failures", poll_s=0)
    assert arn.endswith(":recipe-consumer-failures")


@mock_aws
def test_a_queue_name_that_stays_refused_is_an_error():
    sqs = boto3.client("sqs", region_name=REGION)
    refuse(sqs, "create_queue", "AWS.SimpleQueueService.QueueDeletedRecently", times=99)
    with pytest.raises(ClientError):
        infra.create_queue(sqs, "recipe-consumer-failures", recreate_wait_s=0, poll_s=0)


# ---------- a second run over what --keep left

@mock_aws
def test_a_second_deploy_uses_what_is_there_and_makes_nothing_twice():
    s = boto3.Session(region_name=REGION)
    first = deploy_all(s)
    before = inventory(s)
    second = deploy_all(s)
    assert second["mapping"] == first["mapping"]
    assert inventory(s) == before


def test_an_existing_mapping_is_given_the_settings_and_used():
    """Lambda refuses a second mapping of one source to one function (moto does not): none may be asked for."""
    calls = []

    class Lam:
        def create_event_source_mapping(self, **kw):
            raise ClientError({"Error": {"Code": "ResourceConflictException", "Message": "exists"}}, "Create")

        def list_event_source_mappings(self, **kw):
            assert kw == {"FunctionName": "recipe-consumer", "EventSourceArn": "arn:stream"}
            return {"EventSourceMappings": [{"UUID": "old", "State": "Deleting"}, {"UUID": "abc", "State": "Enabled"}]}

        def get_event_source_mapping(self, **kw):
            return {"UUID": kw["UUID"], "State": "Enabled"}

        def update_event_source_mapping(self, **kw):
            calls.append(kw)

    assert infra.kinesis_mapping(Lam(), "recipe-consumer", "arn:stream", "arn:queue") == "abc"
    assert calls[0]["UUID"] == "abc" and calls[0]["MaximumRetryAttempts"] == 3
    assert "StartingPosition" not in calls[0] and "EventSourceArn" not in calls[0]


def test_a_mapping_that_is_still_changing_is_waited_out_before_the_update(monkeypatch):
    """Lambda refuses an update while a mapping is Creating or Updating: wait for Enabled or Disabled first."""
    monkeypatch.setattr(infra.time, "sleep", lambda s: None)
    states, events = ["Creating", "Updating", "Disabled"], []

    class Lam:
        def list_event_source_mappings(self, **kw):
            return {"EventSourceMappings": [{"UUID": "abc", "State": "Creating"}]}

        def get_event_source_mapping(self, **kw):
            events.append("get")
            return {"UUID": kw["UUID"], "State": states.pop(0)}

        def update_event_source_mapping(self, **kw):
            assert not states, "updated before the mapping was Enabled or Disabled"
            events.append("update")

    assert infra.create_mapping(Lam(), "recipe-history", "arn:queue", BatchSize=10) == "abc"
    assert events == ["get", "get", "get", "update"]


@mock_aws
def test_time_to_live_is_not_enabled_twice():
    ddb = boto3.client("dynamodb", region_name=REGION)
    infra.create_table(ddb, "recipe-windows", "pk", poll_s=0)
    infra.expire_items(ddb, "recipe-windows", "expires_at")
    refuse(ddb, "update_time_to_live", "ValidationException", times=99)   # what DynamoDB answers the second time
    infra.expire_items(ddb, "recipe-windows", "expires_at")
    assert ddb.describe_time_to_live(TableName="recipe-windows")["TimeToLiveDescription"]["TimeToLiveStatus"] == \
        "ENABLED"
