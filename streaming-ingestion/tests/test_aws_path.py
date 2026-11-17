"""The --target aws path, run against moto: every resource is created with the settings the README names, and
clean_up.py removes all of it. Lambda functions are created but not invoked (moto would need Docker for that)."""

import boto3
from moto import mock_aws

import clean_up
import infra

PREFIX = "recipe-test"


def load(name, rel):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@mock_aws
def test_deploy_everything_then_clean_up():
    s = boto3.Session(region_name="ca-central-1")
    n = infra.names(PREFIX)
    made = infra.deploy_main(s, PREFIX)
    load("windowed_run", "ext-windowed-aggregates/run.py").deploy(s, PREFIX, "drop", 120, 25)
    load("fanout_run", "ext-fan-out/run.py").make_topic_and_queues(s, PREFIX)

    lam = s.client("lambda")
    m = lam.get_event_source_mapping(UUID=made["mapping"])
    assert m["BatchSize"] == 100 and m["MaximumRetryAttempts"] == 3 and m["BisectBatchOnFunctionError"]
    assert m["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert m["DestinationConfig"]["OnFailure"]["Destination"].endswith(n["consumer_failures"])
    cfg = lam.get_function(FunctionName=n["consumer"])["Configuration"]
    assert cfg["Runtime"] == "python3.12" and cfg["Architectures"] == ["arm64"]
    assert all(f["FunctionName"].startswith(PREFIX + "-") for f in lam.list_functions()["Functions"])

    clean_up.clean(s, PREFIX)

    assert lam.list_functions()["Functions"] == []
    assert lam.list_event_source_mappings()["EventSourceMappings"] == []
    assert s.client("dynamodb").list_tables()["TableNames"] == []
    assert s.client("kinesis").list_streams()["StreamNames"] == []
    assert s.client("sqs").list_queues().get("QueueUrls", []) == []
    assert s.client("sns").list_topics()["Topics"] == []
    assert [r for r in s.client("iam").list_roles()["Roles"] if r["RoleName"].startswith(PREFIX)] == []
    clean_up.clean(s, PREFIX)                                           # a second run finds nothing and does not fail


@mock_aws
def test_clean_up_leaves_other_prefixes():
    s = boto3.Session(region_name="ca-central-1")
    infra.create_table(s.client("dynamodb"), "recipe-ana-updates", "update_id")
    clean_up.clean(s, "recipe")
    assert s.client("dynamodb").list_tables()["TableNames"] == ["recipe-ana-updates"]


@mock_aws
def test_an_existing_role_is_used_and_none_is_created(monkeypatch):
    s = boto3.Session(region_name="ca-central-1")
    monkeypatch.setenv("LAMBDA_ROLE_ARN", "arn:aws:iam::111122223333:role/given")
    assert infra.create_role(s.client("iam"), "recipe-x-role", [], "ca-central-1", "111122223333", "f") == \
        "arn:aws:iam::111122223333:role/given"
    assert s.client("iam").list_roles()["Roles"] == []
