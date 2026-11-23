"""What the live teardown of ext-backfill and s3-event-etl (2026-11-23) found, offline in moto: the kinds it left
for a person, a ledger row without an ARN, the IAM route dropped by one refused role, and a steps file per system."""
import importlib.util
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import inventory
import kinds
import tags

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def team(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFIX", "t00")
    monkeypatch.setenv("TEAM", "t00")
    monkeypatch.setenv("RESOURCES_DOC", str(tmp_path / "resources.md"))


def test_glue_job_sns_topic_schedule_and_notification_entry_are_asked_for_and_deleted(aws, team):
    account = aws.client("sts").get_caller_identity()["Account"]
    glue, sns, sch, s3 = aws.client("glue"), aws.client("sns"), aws.client("scheduler"), aws.client("s3")
    glue.create_job(Name="t00-etl", Role="arn:aws:iam::123456789012:role/r", Command={"Name": "glueetl",
                                                                                       "ScriptLocation": "s3://b/x"})
    sns.create_topic(Name="t00-etl-alerts")
    sch.create_schedule_group(Name="t00-g")
    sch.create_schedule(Name="hourly", GroupName="t00-g", ScheduleExpression="rate(1 hour)",
                        FlexibleTimeWindow={"Mode": "OFF"},
                        Target={"Arn": "arn:aws:lambda:ca-central-1:123456789012:function:f", "RoleArn": "arn:aws:iam::123456789012:role/r"})
    bucket = f"t00-lake-{account}"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    aws.client("sqs").create_queue(QueueName="q")
    q = f"arn:aws:sqs:ca-central-1:{account}:q"
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [
        {"Id": "t00-s3-event-etl", "QueueArn": q, "Events": ["s3:ObjectCreated:*"]},
        {"Id": "quality-gate", "QueueArn": q, "Events": ["s3:ObjectRemoved:*"]}]})
    for kind, name in (("glue-job", "t00-etl"), ("sns-topic", "t00-etl-alerts"), ("schedule", "t00-g/hourly"),
                       ("s3-notification", "t00-s3-event-etl")):
        k = kinds.KINDS[kind]
        assert k.exists(aws, name), kind
        k.delete(aws, name)
        assert not k.exists(aws, name), kind
    left = s3.get_bucket_notification_configuration(Bucket=bucket)["QueueConfigurations"]
    assert [e["Id"] for e in left] == ["quality-gate"]                     # the other entry is kept


def test_a_ledger_row_without_an_arn_merges_with_the_tag_route_and_has_its_own_name(aws, team):
    tags.record("glue-job", "t00-etl", "created")                          # s3-event-etl records no ARN
    rows = {r.arn: r for r in inventory._ledger("123456789012").values()}
    arn = "arn:aws:glue:ca-central-1:123456789012:job/t00-etl"
    assert arn in rows and rows[arn].id == "t00-etl"
    inventory._add(rows, arn, "tag")
    assert len(rows) == 1 and rows[arn].found_by == ["ledger", "tag"]
    assert inventory.Row("s3-notification", "t00-s3-event-etl", "s3-notification:t00-s3-event-etl").id == \
        "t00-s3-event-etl"                                                 # not an ARN: its name


def test_a_role_whose_tags_are_refused_does_not_drop_the_iam_route(aws, team, monkeypatch):
    iam = aws.client("iam")
    trust = json.dumps({"Version": "2012-10-17", "Statement": []})
    iam.create_role(RoleName="InnovationSandbox-lease-role", AssumeRolePolicyDocument=trust)
    iam.create_role(RoleName="t00-role", AssumeRolePolicyDocument=trust, Tags=[{"Key": "Team", "Value": "t00"}])
    real_client = aws.client

    def client(name, **kw):
        c = real_client(name, **kw)
        if name == "iam":
            real = c.list_role_tags

            def list_role_tags(RoleName):
                if RoleName.startswith("InnovationSandbox"):
                    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "explicit deny"}}, "ListRoleTags")
                return real(RoleName=RoleName)
            c.list_role_tags = list_role_tags
        return c
    monkeypatch.setattr(aws, "client", client)
    lines = []
    rows = inventory.take(aws, log=lines.append)
    assert [r.name for r in rows if "IAM" in r.found_by] == ["t00-role"]
    assert not [ln for ln in lines if ln.startswith("refused")]
    assert lines == ["skipped  1 IAM roles whose tags are refused (the account's own, not the team's)"]


def test_a_steps_file_per_system(tmp_path):
    spec = importlib.util.spec_from_file_location("teardown_lf", ROOT / "scripts" / "teardown.py")
    teardown = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(teardown)
    for name in ("teardown-backfill.toml", "teardown-s3etl.toml"):
        steps = teardown.steps(ROOT / "scripts" / name)
        assert steps[-1]["command"] == "scripts/lake.py down --yes" and steps[0]["command"].startswith("cd ../")
