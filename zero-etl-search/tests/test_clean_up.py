"""clean_up.py removes what a run creates and nothing else; a second run finds nothing; a service the account
denies does not stop the other steps (moto for AWS, a small fake for what moto lacks in OpenSearch Serverless)."""

import io
import zipfile

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import clean_up as cu
import zero_etl
from clean_up import clean_up
from main import infra
from main.config import Settings, load_items
from main.infra import ROLE_SUFFIXES, ensure_role
from pipeline import example_settings, render
from worker import create_queues


def _error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} from the fake"}}, op)


class FakeAoss:
    """OpenSearch Serverless as create_collection leaves it (moto does not cover groups and access policies)."""

    def __init__(self, names, groups=()):
        self.collections, self.policies = set(names), {(n, t) for n in names for t in ("data", "network", "encryption")}
        self.groups = set(groups)

    def left(self) -> list:
        return sorted(self.collections) + sorted(self.policies) + sorted(self.groups)

    def batch_get_collection(self, names):
        return {"collectionDetails": [{"id": n, "name": n} for n in names if n in self.collections]}

    def delete_collection(self, id):
        self.collections.discard(id)

    def get_access_policy(self, name, type):
        if (name, type) not in self.policies:
            raise _error("ResourceNotFoundException", "GetAccessPolicy")

    get_security_policy = get_access_policy

    def delete_access_policy(self, name, type):
        self.policies.discard((name, type))

    delete_security_policy = delete_access_policy

    def batch_get_collection_group(self, names):
        return {"collectionGroupDetails": [{"id": n} for n in names if n in self.groups]}

    def delete_collection_group(self, id):
        self.groups.discard(id)


def create_everything(s: Settings, with_extensions: bool = True) -> FakeAoss:
    """What main/run.py --target aws leaves in an account (and, with_extensions, ext-embeddings and ext-zero-etl),
    made with the recipe's own create functions where moto has the service."""
    account = boto3.client("sts").get_caller_identity()["Account"]
    stream = infra.create_table(boto3.client("dynamodb"), s, load_items())
    _, queue_arn = infra.create_failure_queue(boto3.client("sqs"), s, sleep=lambda _: None)
    iam = boto3.client("iam")
    role = ensure_role(iam, f"{s.prefix}{ROLE_SUFFIXES[0]}", "lambda.amazonaws.com",
                       infra.indexer_statements(s, account, stream, queue_arn), sleep=lambda _: None)
    infra.create_log_group(boto3.client("logs"), s)
    lam = boto3.client("lambda")
    infra.create_function(lam, s, role, "https://abcdefgh1234.ca-central-1.aoss.amazonaws.com")
    infra.create_mapping(lam, s, stream, queue_arn, wait_s=5)
    names = [s.collection]
    if with_extensions:
        zero_etl.create_bucket(boto3.client("s3"), s.bucket(account), s.region)
        for key in (f"{s.prefix}/ddb-export/x.json.gz", f"{s.prefix}/osis-dlq/y.json"):
            boto3.client("s3").put_object(Bucket=s.bucket(account), Key=key, Body=b"x")
        for suffix, service in zip(ROLE_SUFFIXES[1:], ("lambda.amazonaws.com", "osis-pipelines.amazonaws.com")):
            ensure_role(iam, f"{s.prefix}{suffix}", service, [
                {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": "*"}], sleep=lambda _: None)
        create_queues(boto3.client("sqs"), s)
        _, ex = example_settings()
        boto3.client("osis").create_pipeline(PipelineName=s.pipeline, MinUnits=1, MaxUnits=2,
                                             PipelineConfigurationBody=render(s, **ex))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("enqueue.py", "def handler(event, context):\n    return {}\n")
        fn = f"{s.prefix}-reembed-enqueue"
        lam.create_function(FunctionName=fn, Runtime="python3.12", Role=role, Handler="enqueue.handler",
                            Code={"ZipFile": buf.getvalue()})
        lam.create_event_source_mapping(FunctionName=fn, EventSourceArn=stream, StartingPosition="TRIM_HORIZON")
        boto3.client("logs").create_log_group(logGroupName=f"/aws/lambda/{fn}")   # Lambda makes it on the first run
        names.append(s.vector_collection)
    return FakeAoss(names, [s.group] + ([s.vector_group] if with_extensions else []))


def inventory(s: Settings, aoss: FakeAoss) -> dict:
    """Everything in the (moto) account that this recipe could have made."""
    found = {
        "tables": boto3.client("dynamodb").list_tables()["TableNames"],
        "buckets": [b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]],
        "queues": boto3.client("sqs").list_queues().get("QueueUrls", []),
        "pipelines": [p["PipelineName"] for p in boto3.client("osis").list_pipelines()["Pipelines"]],
        "functions": [f["FunctionName"] for f in boto3.client("lambda").list_functions()["Functions"]],
        "mappings": [m["UUID"] for m in boto3.client("lambda").list_event_source_mappings()["EventSourceMappings"]],
        "log groups": [g["logGroupName"] for g in boto3.client("logs").describe_log_groups()["logGroups"]],
        "roles": [r["RoleName"] for r in boto3.client("iam").list_roles()["Roles"]],
        "opensearch serverless": aoss.left(),
    }
    return {k: v for k, v in found.items() if v}


def test_clean_up_leaves_nothing_and_a_second_run_finds_nothing():
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        before = inventory(s, aoss)
        assert set(before) == {"tables", "buckets", "queues", "pipelines", "functions", "mappings", "log groups",
                               "roles", "opensearch serverless"}
        assert len(before["functions"]) == 2 and len(before["queues"]) == 3 and len(before["roles"]) == 3

        planned = clean_up(s, dry_run=True, aoss=aoss)
        assert inventory(s, aoss) == before                                 # a dry run deletes nothing
        assert any(p.startswith("pipeline ") for p in planned.removed)
        assert sum(p.startswith("collection ") for p in planned.removed) == 4  # two collections and their two groups

        steps = clean_up(s, wait_s=5, aoss=aoss)
        assert steps.failed == [] and steps.removed == planned.removed
        assert inventory(s, aoss) == {}
        for fn in (s.function, f"{s.prefix}-reembed-enqueue"):
            assert f"log group /aws/lambda/{fn}" in steps.removed and f"Lambda function {fn}" in steps.removed
        assert f"queue {s.failure_queue}" in steps.removed

        again = clean_up(s, aoss=aoss)
        assert again.removed == [] and again.failed == [] and again.report(log=lambda line: None) == 0


def test_the_main_scenario_alone_is_removed_with_its_three_policies_and_group():
    s = Settings()
    with mock_aws():
        aoss = create_everything(s, with_extensions=False)
        assert set(inventory(s, aoss)) == {"tables", "queues", "functions", "mappings", "log groups", "roles",
                                           "opensearch serverless"}
        steps = clean_up(s, wait_s=5, aoss=aoss)
        assert steps.failed == [] and inventory(s, aoss) == {}
        for kind in ("data access", "network", "encryption"):
            assert f"{kind} policy {s.collection}" in steps.removed
        assert f"collection group {s.group}" in steps.removed and f"IAM role {s.prefix}{ROLE_SUFFIXES[0]}" in steps.removed


def test_the_mapping_has_the_failure_settings():
    s = Settings()
    with mock_aws():
        create_everything(s, with_extensions=False)
        m = boto3.client("lambda").list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"][0]
        assert m["BisectBatchOnFunctionError"] is True and m["MaximumRetryAttempts"] == infra.MAX_RETRIES
        assert m["BatchSize"] == infra.BATCH_SIZE
        assert m["DestinationConfig"]["OnFailure"]["Destination"].endswith(f":{s.failure_queue}")
        assert m["StartingPosition"] == "TRIM_HORIZON"


class _Denied:
    """A client whose every call is refused, with the message a service control policy gives (output/main-aws-denied.txt)
    or a plain one."""

    def __init__(self, scp: bool):
        self.why = ("with an explicit deny in a service control policy" if scp else "no identity-based policy allows it")

    def __getattr__(self, op):
        def call(**kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": f"not authorized: {self.why}"}}, op)
        return call


class _SessionWithout:
    def __init__(self, *denied, scp=False):
        self.denied, self.scp, self.real = denied, scp, boto3.Session(region_name="ca-central-1")

    def client(self, service, **kw):
        return _Denied(self.scp) if service in self.denied else self.real.client(service, **kw)


def test_an_account_that_denies_opensearch_ingestion_cleans_up_without_a_failure():
    """The course sandbox denies osis:* in a service control policy: no pipeline can exist there, so that step is
    "not checked", not FAILED and not "absent" (it was not looked at), and the clean-up exits 0."""
    s = Settings()
    with mock_aws():
        aoss = create_everything(s, with_extensions=False)
        steps = clean_up(s, wait_s=5, aoss=aoss, session=_SessionWithout("osis", scp=True))
        assert steps.failed == [] and inventory(s, aoss) == {}
        assert steps.unchecked == [f"pipeline {s.pipeline} (denied by a service control policy)"]
        assert not any(a.startswith("pipeline") for a in steps.absent) and steps.report(log=lambda line: None) == 0


def test_a_denied_service_is_reported_and_everything_else_is_still_removed():
    """A refused call stopped the clean-up at its first step and left the
    collection, the table, the bucket and the role in the sandbox."""
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        steps = clean_up(s, wait_s=5, aoss=aoss, session=_SessionWithout("osis"))
        assert inventory(s, aoss) == {"pipelines": [s.pipeline]}            # only what the account refused is left
        assert len(steps.failed) == 1
        assert steps.failed[0].startswith(f"pipeline {s.pipeline}: AccessDeniedException")
        lines = []
        assert steps.report(log=lines.append) == 1
        assert sum(line.startswith("FAILED") for line in lines) == 1


def test_two_denied_services_do_not_stop_the_rest_either():
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        steps = clean_up(s, wait_s=5, aoss=aoss, session=_SessionWithout("sqs", "logs"))
        left = inventory(s, aoss)
        assert set(left) == {"queues", "log groups"} and len(steps.failed) == 5   # three queues, two log groups
        assert "tables" not in left and "roles" not in left and "opensearch serverless" not in left


def test_the_command_exits_non_zero_when_a_step_failed(monkeypatch, capsys):
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        session = _SessionWithout("osis")
        session.real_client = session.client
        session.client = lambda service, **kw: aoss if service == "opensearchserverless" else session.real_client(
            service, **kw)
        monkeypatch.setattr(cu.boto3, "Session", lambda **kw: session)
        monkeypatch.setattr("sys.argv", ["clean_up.py"])
        with pytest.raises(SystemExit) as done:
            cu.main()
    out = capsys.readouterr().out
    assert done.value.code == 1 and "1 failed" in out and "run clean_up.py again" in out
    assert f"FAILED   pipeline {s.pipeline}: AccessDeniedException" in out
    assert f"removed  DynamoDB table {s.table}" in out


def test_a_mapping_that_is_already_being_deleted_is_not_a_failure():
    class Lam:
        def get_function(self, FunctionName):
            return {}

        def list_event_source_mappings(self, FunctionName):
            return {"EventSourceMappings": [{"UUID": "m1", "State": "Deleting"}]}

        def delete_event_source_mapping(self, UUID):
            raise _error("ResourceInUseException", "DeleteEventSourceMapping")

        def delete_function(self, FunctionName):
            pass

    class Session(_SessionWithout):
        def client(self, service, **kw):
            return Lam() if service == "lambda" else self.real.client(service, **kw)

    with mock_aws():
        steps = clean_up(Settings(), aoss=FakeAoss([]), session=Session())
    assert steps.failed == [] and "event source mapping m1 (already being deleted)" in steps.absent
    assert "Lambda function test-search-indexer" in steps.removed


def test_a_role_is_created_unless_passed_in_and_only_created_ones_are_removed():
    s = Settings()
    with mock_aws():
        iam = boto3.client("iam")
        given = iam.create_role(RoleName="given", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        assert ensure_role(iam, "x", "lambda.amazonaws.com", [], given) == given
        name = f"{s.prefix}{ROLE_SUFFIXES[0]}"
        arn = ensure_role(iam, name, "lambda.amazonaws.com", [{"Effect": "Allow", "Action": "sqs:SendMessage",
                                                               "Resource": "*"}], sleep=lambda _: None)
        assert arn.endswith(name)
        untagged = f"{s.prefix}{ROLE_SUFFIXES[1]}"                          # same name, not made by the recipe
        iam.create_role(RoleName=untagged, AssumeRolePolicyDocument="{}")
        assert f"IAM role {name}" in clean_up(s, aoss=FakeAoss([])).removed
        assert sorted(r["RoleName"] for r in iam.list_roles()["Roles"]) == sorted(["given", untagged])


def test_a_mapping_left_after_its_function_was_deleted_is_removed():
    """Deleting a function does not delete its event source mappings: clean-up looks them up by function name."""
    s = Settings()
    with mock_aws():
        aoss = create_everything(s, with_extensions=False)
        lam = boto3.client("lambda")
        uuid = lam.list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"][0]["UUID"]

        class FunctionGone:
            def __getattr__(self, op):
                return getattr(lam, op)

            def get_function(self, FunctionName):
                raise _error("ResourceNotFoundException", "GetFunction")

        class Session(_SessionWithout):
            def client(self, service, **kw):
                return FunctionGone() if service == "lambda" else self.real.client(service, **kw)

        steps = clean_up(s, wait_s=5, aoss=aoss, session=Session())
        assert f"event source mapping {uuid}" in steps.removed and f"Lambda function {s.function}" in steps.absent
        assert lam.list_event_source_mappings()["EventSourceMappings"] == []


def test_objects_s3_would_not_delete_are_a_failure_and_only_this_prefix_is_touched():
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        account = boto3.client("sts").get_caller_identity()["Account"]
        s3 = boto3.client("s3")
        s3.put_object(Bucket=s.bucket(account), Key="ana/ddb-export/theirs.json.gz", Body=b"x")   # a teammate's

        class Refusing:
            def __getattr__(self, op):
                return getattr(s3, op)

            def delete_objects(self, Bucket, Delete):
                return {"Deleted": [], "Errors": [{"Key": o["Key"], "Code": "AccessDenied"} for o in Delete["Objects"]]}

        class Session(_SessionWithout):
            def client(self, service, **kw):
                return Refusing() if service == "s3" else self.real.client(service, **kw)

        steps = clean_up(s, wait_s=5, aoss=aoss, session=Session())
        assert sum("not deleted" in f and "AccessDenied" in f for f in steps.failed) == 2
        assert all(f"s3://{s.bucket(account)}/{s.prefix}/" in f for f in steps.failed if "objects" in f)
        steps = clean_up(s, wait_s=5, aoss=aoss)                         # with a working S3, ours go, theirs stay
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=s.bucket(account)).get("Contents", [])]
        assert keys == ["ana/ddb-export/theirs.json.gz"]


def test_a_rerun_updates_the_mapping_and_enables_a_disabled_one():
    s = Settings()
    with mock_aws():
        create_everything(s, with_extensions=False)
        lam = boto3.client("lambda")
        m = lam.list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"][0]
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=False, BatchSize=50, MaximumRetryAttempts=1)
        stream = boto3.client("dynamodb").describe_table(TableName=s.table)["Table"]["LatestStreamArn"]
        queue = boto3.client("sqs").get_queue_attributes(
            QueueUrl=boto3.client("sqs").get_queue_url(QueueName=s.failure_queue)["QueueUrl"],
            AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        lines = []
        assert infra.create_mapping(lam, s, stream, queue, wait_s=5, log=lines.append) == m["UUID"]
        now = lam.get_event_source_mapping(UUID=m["UUID"])
        assert now["State"] == "Enabled" and now["BatchSize"] == infra.BATCH_SIZE
        assert now["MaximumRetryAttempts"] == infra.MAX_RETRIES
        assert now["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
        assert lines and "enabling it again" in lines[0]
