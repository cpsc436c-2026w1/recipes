"""clean_up.py leaves nothing, attempts every step and reports what failed; a run that stops half-way cleans up.
Offline: moto, with the answers moto does not give (a resource still being deleted, a refused call) put in by hand."""

import boto3
import pytest
from moto import mock_aws

import clean_up
import infra
import run as main_run

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


class OneSession:
    """A session that hands out the same client each time, so a test can change one before clean() uses it."""

    def __init__(self):
        self._s, self._clients, self.region_name = boto3.Session(region_name=REGION), {}, REGION

    def client(self, name):
        return self._clients.setdefault(name, self._s.client(name))


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


# ---------- clean-up: nothing left, every step attempted, failures reported

@mock_aws
def test_clean_up_leaves_nothing_and_a_run_after_it_works():
    s = boto3.Session(region_name=REGION)
    empty = inventory(s)
    deploy_all(s)
    groups = [g["logGroupName"] for g in s.client("logs").describe_log_groups()["logGroups"]]
    assert groups == [f"/aws/lambda/{PREFIX}-consumer", f"/aws/lambda/{PREFIX}-windows"]   # made by the deploy
    assert clean_up.clean(s, PREFIX).failed == []
    assert inventory(s) == empty
    deploy_all(s)                                           # a run right after the clean-up
    assert clean_up.clean(s, PREFIX).failed == []
    assert inventory(s) == empty


@mock_aws
def test_a_missing_topic_is_reported_absent_not_removed():
    steps = clean_up.clean(boto3.Session(region_name=REGION), PREFIX)
    assert steps.removed == [] and steps.failed == []
    assert f"topic {PREFIX}-updates-topic" in steps.absent


@mock_aws
def test_a_mapping_that_is_being_deleted_does_not_stop_the_clean_up():
    s = OneSession()
    deploy_all(s)
    lam = s.client("lambda")
    real_list = lam.list_event_source_mappings
    lam.list_event_source_mappings = lambda **kw: {"EventSourceMappings": [
        {**m, "State": "Deleting"} for m in real_list(**kw)["EventSourceMappings"]]}
    refuse(lam, "delete_event_source_mapping", "ResourceInUseException", times=99)   # Lambda's answer for it
    steps = clean_up.clean(s, PREFIX)
    assert steps.failed == []
    assert any("already being deleted" in a for a in steps.absent)
    assert s.client("kinesis").list_streams()["StreamNames"] == []       # the steps after the mapping still ran
    assert s.client("dynamodb").list_tables()["TableNames"] == []


@mock_aws
def test_a_mapping_in_use_for_a_moment_is_tried_again(monkeypatch):
    s = OneSession()
    deploy_all(s)
    lam = s.client("lambda")
    refuse(lam, "delete_event_source_mapping", "ResourceInUseException", times=1)
    real_get = lam.get_event_source_mapping
    lam.get_event_source_mapping = lambda **kw: {**real_get(**kw), "State": "Creating"}
    # clean_up.time is the time module itself: monkeypatch puts the real sleep back after the test, so later tests
    # (the fan-out run sleeps on purpose) are not left with a sleep that returns at once.
    monkeypatch.setattr(clean_up.time, "sleep", lambda s_: None)
    steps = clean_up.clean(s, PREFIX)
    assert steps.failed == [] and lam.list_event_source_mappings()["EventSourceMappings"] == []


@mock_aws
def test_a_refused_delete_is_reported_and_the_other_steps_still_run(capsys):
    s = OneSession()
    deploy_all(s)
    refuse(s.client("lambda"), "list_event_source_mappings", "AccessDeniedException", times=99)
    refuse(s.client("sqs"), "get_queue_url", "AccessDenied", times=1)
    steps = clean_up.clean(s, PREFIX)
    assert len(steps.failed) == 5                                        # four functions' mappings and one queue
    assert all("AccessDenied" in f for f in steps.failed)
    assert s.client("lambda").list_functions()["Functions"] == []
    assert s.client("dynamodb").list_tables()["TableNames"] == []
    assert s.client("kinesis").list_streams()["StreamNames"] == []
    out = capsys.readouterr().out
    assert "FAILED" in out and "NOT complete" in out


@mock_aws
def test_a_table_or_stream_that_is_being_deleted_is_not_deleted_again():
    s = OneSession()
    deploy_all(s)
    ddb, kinesis = s.client("dynamodb"), s.client("kinesis")
    ddb.describe_table = lambda **kw: {"Table": {"TableStatus": "DELETING"}}
    kinesis.describe_stream_summary = lambda **kw: {"StreamDescriptionSummary": {"StreamStatus": "DELETING"}}
    # DeleteTable on a table in DELETING returns no error (DynamoDB API reference, DeleteTable); DeleteStream on a
    # stream in DELETING is refused, since it needs an ACTIVE stream (Kinesis API reference, DeleteStream)
    ddb.delete_table = lambda **kw: {"TableDescription": {"TableName": kw["TableName"], "TableStatus": "DELETING"}}
    refuse(kinesis, "delete_stream", "ResourceInUseException", times=99)
    steps = clean_up.clean(s, PREFIX)
    assert steps.failed == []
    assert f"stream {PREFIX}-updates (already being deleted)" in steps.absent
    assert f"table {PREFIX}-updates (already being deleted)" in steps.absent


# ---------- a run that stops half-way removes what it made

@mock_aws
def test_a_run_that_fails_while_deploying_still_cleans_up(monkeypatch):
    def broken(lam, uuid, **kw):
        raise TimeoutError("mapping not Enabled")
    monkeypatch.setattr(infra, "wait_mapping", broken)
    monkeypatch.setenv("PREFIX", PREFIX)
    with pytest.raises(TimeoutError):
        main_run.run_aws(5, PREFIX, rate=0, wait_s=0, keep=False)
    s = boto3.Session(region_name=REGION)
    assert s.client("kinesis").list_streams()["StreamNames"] == []
    assert s.client("lambda").list_functions()["Functions"] == []
    assert s.client("dynamodb").list_tables()["TableNames"] == []
