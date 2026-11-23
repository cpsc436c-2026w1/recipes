"""clean_up.py removes what a run creates and nothing else, attempts every step, and says what failed.

Offline: moto, with the answers moto does not give (a resource still being deleted, a refused call) put in by hand.
"""

import boto3
from moto import mock_aws

import clean_up as clean_up_module
from clean_up import clean_up
from main.config import Settings
from main.infra import create_bucket, create_firehose, create_function, create_table, ensure_roles, role_names


def refuse(client, operation, code, times=1):
    """Make the next `times` calls of client.<operation> fail with this error code, then behave as before."""
    real, left = getattr(client, operation), [times]

    def call(**kw):
        if left[0] > 0:
            left[0] -= 1
            raise client.exceptions.from_code(code)({"Error": {"Code": code, "Message": "put in by the test"}},
                                                    operation)
        return real(**kw)
    setattr(client, operation, call)


class OneSession:
    """A session that hands out the same client each time, so a test can change one before clean_up uses it."""

    def __init__(self):
        self._clients = {}

    def client(self, name, region_name=None):
        return self._clients.setdefault(name, boto3.client(name, region_name=region_name))


def inventory() -> dict:
    c = boto3.client
    return {
        "schedules": [x["Name"] for x in c("scheduler").list_schedules()["Schedules"]],
        "alarms": [a["AlarmName"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"]],
        "mappings": [m["UUID"] for m in c("lambda").list_event_source_mappings()["EventSourceMappings"]],
        "functions": [f["FunctionName"] for f in c("lambda").list_functions()["Functions"]],
        "log groups": [g["logGroupName"] for g in c("logs").describe_log_groups()["logGroups"]],
        "firehose": c("firehose").list_delivery_streams()["DeliveryStreamNames"],
        "glue": [d["Name"] for d in c("glue").get_databases()["DatabaseList"]],
        "buckets": [b["Name"] for b in c("s3").list_buckets()["Buckets"]],
        "tables": c("dynamodb").list_tables()["TableNames"],
        "roles": [r["RoleName"] for r in c("iam").list_roles()["Roles"]],
    }


def create_everything(s: Settings) -> Settings:
    """What main/ and the three extensions create on AWS, with the recipe's own functions where moto can run them."""
    account = boto3.client("sts").get_caller_identity()["Account"]
    stream_arn = create_table(boto3.client("dynamodb"), s, poll_s=0)
    s = ensure_roles(boto3.client("iam"), s, account, stream_arn, which=("lambda", "firehose", "scheduler"),
                     sleep=lambda _: None)
    s3 = boto3.client("s3")
    create_bucket(s3, s.bucket(account), s.region)
    for key in ("lake/trip_changes/data/x.parquet", "athena-results/q.csv", "firehose-errors/e", "freshness/f.json"):
        s3.put_object(Bucket=s.bucket(account), Key=key, Body=b"x")
    create_firehose(boto3.client("firehose"), s, account, poll_s=0)
    create_function(boto3.client("lambda"), s, stream_arn)
    if boto3.client("glue").get_databases()["DatabaseList"]:
        return s                                # a second run: the parts below are made by hand, once
    boto3.client("glue").create_database(DatabaseInput={"Name": s.database})
    boto3.client("glue").create_table(DatabaseName=s.database, TableInput={"Name": s.lake_table})
    boto3.client("glue").create_table(DatabaseName=s.database, TableInput={"Name": s.view})
    boto3.client("logs").create_log_group(logGroupName=f"/aws/lambda/{s.function}")   # Lambda makes it on a call
    for name in ("test-cdc-lag", "test-cdc-iterator-age"):
        boto3.client("cloudwatch").put_metric_alarm(
            AlarmName=name, MetricName="m", Namespace="n", Statistic="Maximum", Period=60,
            EvaluationPeriods=1, Threshold=1.0, ComparisonOperator="GreaterThanThreshold")
    for name in ("test-lake-optimize", "test-lake-vacuum"):
        boto3.client("scheduler").create_schedule(
            Name=name, ScheduleExpression="cron(0 3 * * ? *)", FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": "arn:aws:scheduler:::aws-sdk:athena:startQueryExecution", "RoleArn": s.scheduler_role_arn})
    return s


def test_clean_up_removes_everything_once():
    with mock_aws():
        iam = boto3.client("iam")
        role = iam.create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        s = Settings(lambda_role_arn=role)
        account = boto3.client("sts").get_caller_identity()["Account"]
        stream_arn = create_table(boto3.client("dynamodb"), s)
        create_bucket(boto3.client("s3"), s.bucket(account), s.region)
        boto3.client("s3").put_object(Bucket=s.bucket(account), Key="lake/trip_changes/data/x.parquet", Body=b"x")
        boto3.client("s3").create_bucket(Bucket="someone-else",
                                         CreateBucketConfiguration={"LocationConstraint": s.region})
        boto3.client("glue").create_database(DatabaseInput={"Name": s.database})
        boto3.client("glue").create_table(DatabaseName=s.database, TableInput={"Name": s.lake_table})
        create_function(boto3.client("lambda"), s, stream_arn)
        boto3.client("cloudwatch").put_metric_alarm(
            AlarmName="test-cdc-lag", MetricName="m", Namespace="n", Statistic="Maximum", Period=60,
            EvaluationPeriods=1, Threshold=1.0, ComparisonOperator="GreaterThanThreshold")

        # alarm, mapping, function, table, database, objects, bucket, DynamoDB
        assert len(clean_up(s, dry_run=True).removed) == 8
        steps = clean_up(s)
        assert steps.failed == []
        assert any("DynamoDB table test-trips" in r for r in steps.removed)
        assert any(r.startswith("bucket ") for r in steps.removed)
        again = clean_up(s)
        assert again.removed == [] and again.failed == []
        assert [b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]] == ["someone-else"]


def test_roles_are_created_unless_passed_in_and_only_created_ones_are_removed():
    with mock_aws():
        iam = boto3.client("iam")
        given = iam.create_role(RoleName="given", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        s = ensure_roles(iam, Settings(prefix="t", lambda_role_arn=given), "111122223333", "arn:stream",
                         sleep=lambda _: None)
        assert s.lambda_role_arn == given                                  # passed in: used as it is
        assert s.firehose_role_arn.endswith(role_names(s)["firehose"])     # not passed in: created
        doc = iam.get_role_policy(RoleName=role_names(s)["firehose"], PolicyName="least-privilege")
        assert "glue:UpdateTable" in str(doc["PolicyDocument"])
        removed = clean_up(s).removed
        assert f"IAM role {role_names(s)['firehose']}" in removed
        assert sorted(r["RoleName"] for r in iam.list_roles()["Roles"]) == ["given"]


def test_nothing_is_left_after_a_clean_up_and_a_run_after_it_works():
    with mock_aws():
        empty = inventory()
        s = create_everything(Settings())
        assert all(inventory()[kind] for kind in empty), "the test must create one of each kind"
        steps = clean_up(s)
        assert steps.failed == []
        assert inventory() == empty
        create_everything(Settings())                       # a run right after the clean-up
        assert clean_up(s).failed == []
        assert inventory() == empty


def test_a_second_run_uses_what_is_there():
    with mock_aws():
        create_everything(Settings())
        before = inventory()
        create_everything(Settings())
        assert inventory() == before


def test_the_function_log_group_is_removed_also_when_the_function_is_gone():
    """/aws/lambda/$PREFIX-cdc-to-firehose must not stay after the clean-up."""
    with mock_aws():
        s = Settings()
        logs = boto3.client("logs")
        logs.create_log_group(logGroupName=f"/aws/lambda/{s.function}")
        logs.create_log_group(logGroupName=f"/aws/lambda/{s.function}-of-a-teammate")
        steps = clean_up(s)
        assert steps.removed == [f"log group /aws/lambda/{s.function}"]
        assert [g["logGroupName"] for g in logs.describe_log_groups()["logGroups"]] == [
            f"/aws/lambda/{s.function}-of-a-teammate"]


def test_a_refused_step_is_reported_and_the_steps_after_it_still_run():
    with mock_aws():
        s = create_everything(Settings())
        session = OneSession()
        refuse(session.client("scheduler"), "delete_schedule", "AccessDeniedException", times=1)
        refuse(session.client("cloudwatch"), "describe_alarms", "AccessDenied", times=1)
        steps = clean_up(s, session=session)
        assert len(steps.failed) == 2 and "schedule test-lake-optimize" in steps.failed[0]
        left = inventory()
        assert left["schedules"] == ["test-lake-optimize"] and len(left["alarms"]) == 2
        for kind in ("mappings", "functions", "log groups", "firehose", "glue", "buckets", "tables", "roles"):
            assert left[kind] == [], kind                    # everything after the refused steps was still removed


def test_a_bucket_that_may_not_be_read_is_a_failure_not_a_silent_skip():
    with mock_aws():
        s = create_everything(Settings())
        session = OneSession()
        refuse(session.client("s3"), "head_bucket", "403", times=1)
        steps = clean_up(s, session=session)
        assert len(steps.failed) == 1 and steps.failed[0].startswith("bucket ")
        assert inventory()["buckets"] != []


def test_objects_s3_did_not_delete_are_a_failure_and_the_bucket_is_kept():
    with mock_aws():
        s = create_everything(Settings())
        session = OneSession()
        s3 = session.client("s3")
        s3.delete_objects = lambda **kw: {"Errors": [{"Key": kw["Delete"]["Objects"][0]["Key"],
                                                     "Code": "AccessDenied", "Message": "no"}]}
        steps = clean_up(s, session=session)
        assert len(steps.failed) == 4 and all("AccessDenied" in f for f in steps.failed)   # the four prefixes
        assert not any(r.startswith("bucket ") for r in steps.removed)


def test_resources_that_are_already_being_deleted_do_not_fail_the_clean_up():
    with mock_aws():
        s = create_everything(Settings())
        session = OneSession()
        lam, fh, ddb = session.client("lambda"), session.client("firehose"), session.client("dynamodb")
        real_list = lam.list_event_source_mappings
        lam.list_event_source_mappings = lambda **kw: {"EventSourceMappings": [
            {**m, "State": "Deleting"} for m in real_list(**kw)["EventSourceMappings"]]}
        fh.describe_delivery_stream = lambda **kw: {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}}
        ddb.describe_table = lambda **kw: {"Table": {"TableStatus": "DELETING"}}
        for client, operation in ((lam, "delete_event_source_mapping"), (fh, "delete_delivery_stream")):
            refuse(client, operation, "ResourceInUseException", times=99)     # what AWS answers for each
        # DeleteTable on a table in DELETING returns no error (DynamoDB API reference, DeleteTable)
        ddb.delete_table = lambda **kw: {"TableDescription": {"TableName": kw["TableName"], "TableStatus": "DELETING"}}
        steps = clean_up(s, session=session)
        assert steps.failed == []
        assert sum("already being deleted" in a for a in steps.absent) == 3


def test_a_mapping_in_use_for_a_moment_is_tried_again(monkeypatch):
    with mock_aws():
        s = create_everything(Settings())
        session = OneSession()
        lam = session.client("lambda")
        refuse(lam, "delete_event_source_mapping", "ResourceInUseException", times=1)
        real_get = lam.get_event_source_mapping
        lam.get_event_source_mapping = lambda **kw: {**real_get(**kw), "State": "Creating"}
        monkeypatch.setattr(clean_up_module.time, "sleep", lambda _: None)
        assert clean_up(s, session=session).failed == []
        assert inventory()["mappings"] == []


def test_the_command_exits_non_zero_when_a_step_failed(monkeypatch, capsys):
    with mock_aws():
        monkeypatch.setattr("sys.argv", ["clean_up.py", "--yes"])
        assert clean_up_module.main() == 0
        create_everything(Settings())
        real_session = boto3.Session

        def session(**kw):
            one = OneSession()
            refuse(one.client("dynamodb"), "delete_table", "AccessDeniedException", times=1)
            return one
        monkeypatch.setattr(boto3, "Session", session)
        assert clean_up_module.main() == 1
        monkeypatch.setattr(boto3, "Session", real_session)
        out = capsys.readouterr().out
        assert "FAILED   DynamoDB table test-trips" in out and "NOT complete" in out


def test_only_the_alarms_the_extension_makes_are_deleted():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("lag", Path(__file__).parent.parent / "ext-lag-alarm" / "lag.py")
    lag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lag)
    with mock_aws():
        s = Settings()
        cw = boto3.client("cloudwatch")
        made = lag.put_alarms(cw, s)
        assert sorted(made) == sorted(f"{s.prefix}-{n}" for n in clean_up_module.LAG_ALARMS)
        cw.put_metric_alarm(AlarmName=f"{s.prefix}-cdc-my-own", MetricName="m", Namespace="n", Statistic="Maximum",
                            Period=60, EvaluationPeriods=1, Threshold=1.0, ComparisonOperator="GreaterThanThreshold")
        steps = clean_up(s)
        assert any(r.startswith("alarms ") for r in steps.removed)
        assert [a["AlarmName"] for a in cw.describe_alarms()["MetricAlarms"]] == [f"{s.prefix}-cdc-my-own"]
