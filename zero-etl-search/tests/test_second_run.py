"""A second run, and a run right after clean_up.py, succeed: what already exists is used, and what is still
being deleted is waited for (fakes play the states moto does not have: DELETING, QueueDeletedRecently)."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import main.infra as infra
import worker
from clean_up import clean_up
from main.config import Settings, load_items
from test_clean_up import create_everything, inventory


def test_everything_can_be_created_twice_and_again_after_a_clean_up():
    s = Settings()
    with mock_aws():
        aoss = create_everything(s)
        first = inventory(s, aoss)
        stream = infra.create_table(boto3.client("dynamodb"), s, load_items())   # a second run: nothing new, no error
        _, queue_arn = infra.create_failure_queue(boto3.client("sqs"), s)
        worker.create_queues(boto3.client("sqs"), s)
        role = infra.ensure_role(boto3.client("iam"), f"{s.prefix}{infra.ROLE_SUFFIXES[0]}", "lambda.amazonaws.com",
                                 [{"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": "*"}],
                                 sleep=lambda _: None)
        infra.create_log_group(boto3.client("logs"), s)
        infra.create_function(boto3.client("lambda"), s, role, "https://abcdefgh1234.example")  # updates in place
        infra.create_mapping(boto3.client("lambda"), s, stream, queue_arn, wait_s=5)            # finds the one there
        assert inventory(s, aoss) == first
        assert clean_up(s, wait_s=5, aoss=aoss).failed == []
        again = create_everything(s)                                         # right after the clean-up
        assert {k: len(v) for k, v in inventory(s, again).items()} == {k: len(v) for k, v in first.items()}


class _NotFound(Exception):
    pass


class _InUse(Exception):
    pass


class DeletingTable:
    """DynamoDB while an earlier clean-up's delete_table is still running: describe says DELETING, create says
    'in use', and after the waiter the table is gone."""

    class exceptions:  # noqa: N801 - the name boto3 clients use
        ResourceNotFoundException = _NotFound
        ResourceInUseException = _InUse

    def __init__(self):
        self.status, self.calls = "DELETING", []

    def describe_table(self, TableName):
        if self.status is None:
            raise _NotFound()
        return {"Table": {"TableStatus": self.status, "TableArn": f"arn:aws:dynamodb:ca-central-1:1:table/{TableName}"}}

    def create_table(self, **kw):
        self.calls.append("create_table")
        if self.status is not None:
            raise _InUse()
        self.status = "ACTIVE"

    def update_continuous_backups(self, TableName, PointInTimeRecoverySpecification):
        self.calls.append("update_continuous_backups")

    def get_waiter(self, name):
        ddb = self

        class Waiter:
            def wait(self, TableName):
                ddb.calls.append(name)
                if name == "table_not_exists":
                    ddb.status = None
                elif ddb.status != "ACTIVE":
                    raise RuntimeError("table_exists waited on a table that is being deleted")
        return Waiter()


def test_create_table_waits_for_a_table_that_is_being_deleted(monkeypatch):
    class NoTable:
        def Table(self, name):  # noqa: N802
            raise AssertionError("no items are written in this test")

    ddb = DeletingTable()
    monkeypatch.setattr(infra.boto3, "resource", lambda *a, **kw: NoTable())
    with pytest.raises(AssertionError, match="no items are written"):
        infra.create_table(ddb, Settings(), [{"id": "x"}])
    assert ddb.calls == ["table_not_exists", "create_table", "table_exists", "update_continuous_backups"]
    assert ddb.status == "ACTIVE"


def test_create_queues_waits_out_the_minute_after_a_delete():
    """SQS refuses a queue name for 60 s after a queue of that name was deleted."""
    with mock_aws():
        real = boto3.client("sqs")
        refusals, sleeps = [2], []

        class RecentlyDeleted:
            exceptions = real.exceptions

            def create_queue(self, **kw):
                if refusals[0]:
                    refusals[0] -= 1
                    raise real.exceptions.QueueDeletedRecently(
                        {"Error": {"Code": "AWS.SimpleQueueService.QueueDeletedRecently", "Message": "wait 60 s"}},
                        "CreateQueue")
                return real.create_queue(**kw)

            def __getattr__(self, name):
                return getattr(real, name)

        url, dlq = worker.create_queues(RecentlyDeleted(), Settings(), sleep=sleeps.append)
        assert sleeps == [10, 10] and url.endswith("test-reembed") and dlq.endswith("test-reembed-dlq")

        refusals[0] = 100                                                    # it never clears: give up, loudly
        with pytest.raises(ClientError):
            worker.create_queues(RecentlyDeleted(), Settings(), sleep=sleeps.append)
        assert len(sleeps) == 2 + 7


def test_create_collection_waits_for_a_collection_that_is_being_deleted(monkeypatch):
    class Aoss:
        class exceptions:  # noqa: N801
            ConflictException = _InUse

        def __init__(self):
            self.status, self.polls, self.created = "DELETING", 0, 0

        def create_security_policy(self, **kw):
            pass

        create_access_policy = create_collection_group = create_security_policy

        def create_collection(self, **kw):
            if self.status is not None:
                raise _InUse()
            self.status, self.created = "ACTIVE", self.created + 1

        def batch_get_collection(self, names):
            self.polls += 1
            if self.status == "DELETING" and self.polls > 2:
                self.status = None
            if self.status is None:
                return {"collectionDetails": []}
            return {"collectionDetails": [{"status": self.status, "collectionEndpoint": "https://c.example"}]}

    monkeypatch.setattr(infra.time, "sleep", lambda s: None)
    aoss = Aoss()
    assert infra.create_collection(aoss, Settings(), "test-items", "SEARCH", ["arn"], wait_s=5) == "https://c.example"
    assert aoss.created == 1


def test_a_failed_message_says_why_once_per_reason():
    """`{'failed': 3}` alone does not say whether the model was busy, the key was refused or the code is wrong."""
    s = Settings()
    with mock_aws():
        infra.create_table(boto3.client("dynamodb"), s, load_items())
        sqs = boto3.client("sqs")
        url, _ = worker.create_queues(sqs, s, visibility_s=0)
        table = boto3.resource("dynamodb").Table(s.table)
        from enqueue import text_hash

        for item_id in ("I01", "I02"):
            item = table.get_item(Key={"id": item_id})["Item"]
            sqs.send_message(QueueUrl=url, MessageBody='{"op": "MODIFY", "id": "%s", "text_hash": "%s"}' % (
                item_id, text_hash(item)))

        class Refused:
            model = "m"

            def embed(self, texts):
                raise RuntimeError("HTTP 401 invalid_api_key")

        lines = []
        counts = worker.drain(sqs, url, table, Refused(), worker.StandInVectors(), rounds=1, log=lines.append)
    assert counts == {"failed": 2}
    assert lines == ["failed, left in the queue: RuntimeError: HTTP 401 invalid_api_key"]


def _client_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateCollection")


def test_a_group_that_vanishes_after_already_exists_is_made_again(monkeypatch):
    """The group an earlier clean-up was deleting answers ConflictException and is then gone: the collection create
    is refused for want of it, the group is made again, and the create retried."""
    class Aoss:
        class exceptions:  # noqa: N801
            ConflictException = _InUse

        def __init__(self):
            self.groups, self.created, self.refused = 1, 0, 0

        def create_security_policy(self, **kw):
            pass

        create_access_policy = create_security_policy

        def create_collection_group(self, **kw):
            if self.groups:
                raise _InUse()
            self.groups = 1

        def create_collection(self, **kw):
            if not self.refused:
                self.refused, self.groups = 1, 0                        # the delete finishes
                raise _client_error("ValidationException", "Collection group test-search not found")
            self.created += 1

        def batch_get_collection(self, names):
            return {"collectionDetails": [{"status": "ACTIVE", "collectionEndpoint": "https://c.example"}]
                    if self.created else []}

    monkeypatch.setattr(infra.time, "sleep", lambda s: None)
    aoss = Aoss()
    assert infra.create_collection(aoss, Settings(), "test-items", "SEARCH", ["arn"], wait_s=5) == "https://c.example"
    assert aoss.refused == 1 and aoss.created == 1 and aoss.groups == 1


def test_only_a_refusal_about_the_group_is_retried():
    assert infra.group_gone(_client_error("ResourceNotFoundException", "no such group"), "test-search")
    assert infra.group_gone(_client_error("ValidationException", "Collection group test-search not found"),
                            "test-search")
    assert not infra.group_gone(_client_error("ValidationException", "Invalid collection name"), "test-search")
    assert not infra.group_gone(_client_error("AccessDeniedException", "collection group"), "test-search")


def test_clean_up_waits_until_the_group_is_gone(monkeypatch):
    from test_clean_up import FakeAoss

    class Slow(FakeAoss):
        polls = 0

        def batch_get_collection_group(self, names):
            if names == [Settings().group] and Settings().group not in self.groups and self.deleted:
                self.polls += 1
                return {"collectionGroupDetails": [{"id": "g"}] if self.polls < 3 else []}
            return super().batch_get_collection_group(names)

        deleted = False

        def delete_collection_group(self, id):
            self.deleted = True
            super().delete_collection_group(id)

    import clean_up as cu

    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    aoss = Slow([Settings().collection], [Settings().group])
    with mock_aws():
        steps = clean_up(Settings(), wait_s=60, aoss=aoss)
    assert steps.failed == [] and aoss.polls == 3                       # asked until the group was gone
