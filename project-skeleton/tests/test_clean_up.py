"""clean_up.py removes what the runs create and nothing else, attempts every step, and says what failed.

moto plays AWS; the answers moto does not give (a resource still being deleted, a refused call) are put in by hand.
"""
import json

import clean_up
import lakehouse
import operational
from skeleton.config import names

ACCOUNT = "123456789012"


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
    """A session that hands out the same client each time, so a test can change one before clean_aws uses it."""

    def __init__(self, sess):
        self._sess, self._clients = sess, {}

    def client(self, name):
        return self._clients.setdefault(name, self._sess.client(name))


def inventory(aws) -> dict:
    c = aws.client
    return {
        "streams": c("kinesis").list_streams()["StreamNames"],
        "firehose": c("firehose").list_delivery_streams()["DeliveryStreamNames"],
        "tables": c("dynamodb").list_tables()["TableNames"],
        "functions": [f["FunctionName"] for f in c("lambda").list_functions()["Functions"]],
        "log groups": [g["logGroupName"] for g in c("logs").describe_log_groups()["logGroups"]],
        "buckets": [b["Name"] for b in c("s3").list_buckets()["Buckets"]],
        "glue": [d["Name"] for d in c("glue").get_databases()["DatabaseList"]],
        "workgroups": [w["Name"] for w in c("athena").list_work_groups()["WorkGroups"] if w["Name"] != "primary"],
        "roles": [r["RoleName"] for r in c("iam").list_roles()["Roles"]],
    }


def run_everything(aws, n: dict) -> None:
    """What the three representatives create in AWS (generative AI first only reads operational first's table)."""
    lakehouse.setup(aws, n, ACCOUNT, sleep=lambda s: None)
    operational.setup(aws, n, ACCOUNT, sleep=lambda s: None)
    for key in ("updates/dt=2026-11-12/a.parquet", "exports/AWSDynamoDB/01/data/a.json.gz", "athena-results/q.csv"):
        aws.client("s3").put_object(Bucket=n["bucket"], Key=key, Body=b"x")


def test_clean_up_removes_everything_a_run_creates(aws):
    n = names("c1", account=ACCOUNT)
    empty = inventory(aws)
    run_everything(aws, n)
    assert all(inventory(aws)[kind] for kind in empty), "the test must create one of each kind"

    log = []
    steps = clean_up.clean_aws(aws, n, log=log.append)
    assert steps.failed == [] and steps.absent == []
    assert all(line.startswith("removed") for line in log), log
    assert inventory(aws) == empty

    again = []
    steps = clean_up.clean_aws(aws, n, log=again.append)       # a second run finds nothing and does not fail
    assert steps.failed == [] and steps.removed == []
    assert all(line.startswith("absent") for line in again), again


def test_a_run_right_after_a_clean_up_and_a_second_run_work(aws):
    n = names("c1", account=ACCOUNT)
    empty = inventory(aws)
    run_everything(aws, n)
    clean_up.clean_aws(aws, n, log=lambda s: None)
    run_everything(aws, n)                                     # right after a clean-up
    once = inventory(aws)
    run_everything(aws, n)                                     # and again, over what is there
    assert inventory(aws) == once
    assert clean_up.clean_aws(aws, n, log=lambda s: None).failed == []
    assert inventory(aws) == empty


def test_clean_up_keeps_a_role_it_did_not_create(aws):
    n = names("c2", account=ACCOUNT)
    iam = aws.client("iam")
    iam.create_role(RoleName="c2-api-role", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17",
                                                                                 "Statement": []}))
    steps = clean_up.clean_aws(aws, n, log=lambda s: None)
    assert iam.get_role(RoleName="c2-api-role")["Role"]["RoleName"] == "c2-api-role"
    assert "IAM role c2-api-role (kept: not created by this recipe)" in steps.absent


def test_a_refused_step_is_reported_and_the_steps_after_it_still_run(aws):
    n = names("c1", account=ACCOUNT)
    run_everything(aws, n)
    one = OneSession(aws)
    refuse(one.client("firehose"), "delete_delivery_stream", "AccessDeniedException")
    refuse(one.client("lambda"), "delete_function", "AccessDeniedException")
    log = []
    steps = clean_up.clean_aws(one, n, log=log.append)
    assert len(steps.failed) == 2 and steps.failed[0].startswith("Firehose stream c1-to-lake: AccessDenied")
    left = inventory(aws)
    assert left["firehose"] == ["c1-to-lake"] and left["functions"] == ["c1-api"]
    for kind in ("streams", "tables", "log groups", "buckets", "glue", "workgroups", "roles"):
        assert left[kind] == [], kind                          # the Kinesis stream (shard-hours) went all the same
    assert sum(line.startswith("FAILED") for line in log) == 2


def test_resources_that_are_already_being_deleted_do_not_fail_the_clean_up(aws):
    n = names("c1", account=ACCOUNT)
    run_everything(aws, n)
    one = OneSession(aws)
    fh, kin, ddb = one.client("firehose"), one.client("kinesis"), one.client("dynamodb")
    fh.describe_delivery_stream = lambda **kw: {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}}
    kin.describe_stream_summary = lambda **kw: {"StreamDescriptionSummary": {"StreamStatus": "DELETING"}}
    ddb.describe_table = lambda **kw: {"Table": {"TableStatus": "DELETING"}}
    for client, operation in ((fh, "delete_delivery_stream"), (kin, "delete_stream")):
        refuse(client, operation, "ResourceInUseException", times=99)       # what AWS answers for each
    # DeleteTable on a table in DELETING returns no error (DynamoDB API reference, DeleteTable)
    ddb.delete_table = lambda **kw: {"TableDescription": {"TableName": kw["TableName"], "TableStatus": "DELETING"}}
    steps = clean_up.clean_aws(one, n, log=lambda s: None)
    assert steps.failed == []
    assert sum("already being deleted" in a for a in steps.absent) == 3


def test_the_command_exits_non_zero_when_a_step_failed(aws, monkeypatch, tmp_path, capsys):
    n = names("c1", account=ACCOUNT)
    run_everything(aws, n)
    one = OneSession(aws)
    refuse(one.client("dynamodb"), "delete_table", "AccessDeniedException")
    monkeypatch.setenv("PREFIX", "c1")
    monkeypatch.setattr("sys.argv", ["clean_up.py"])
    monkeypatch.setattr(clean_up, "session", lambda: one)
    monkeypatch.setattr(clean_up, "LOCAL_ROOT", tmp_path / ".local")
    assert clean_up.main() == 1
    assert "NOT complete" in capsys.readouterr().out
    assert clean_up.main() == 0                                # the table goes on the second try


def test_clean_local(monkeypatch, tmp_path):
    root = tmp_path / ".local"
    (root / "x").mkdir(parents=True)
    monkeypatch.setattr(clean_up, "LOCAL_ROOT", root)
    clean_up.clean_local(log=lambda s: None)
    assert not root.exists()
