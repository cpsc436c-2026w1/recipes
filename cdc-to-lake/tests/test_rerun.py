"""A run right after a clean-up: a table or a Firehose stream that is still being deleted is waited out and created
again. Offline: moto, with the answers moto does not give (DELETING, "in use") put in by hand."""

import boto3
import pytest
from moto import mock_aws

from main.config import Settings
from main.infra import create_firehose, create_table


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


def test_a_table_that_is_still_being_deleted_is_waited_out_and_created_again():
    with mock_aws():
        ddb, s = boto3.client("dynamodb"), Settings()
        refuse(ddb, "create_table", "ResourceInUseException")
        real_describe, real_waiter, waited = ddb.describe_table, ddb.get_waiter, []
        ddb.describe_table = lambda **kw: real_describe(**kw) if waited else {"Table": {"TableStatus": "DELETING"}}

        class Gone:
            def wait(self, **kw):
                waited.append(kw["TableName"])
        ddb.get_waiter = lambda name: Gone() if name == "table_not_exists" else real_waiter(name)
        arn = create_table(ddb, s, poll_s=0)
        assert waited == [s.table] and "/stream/" in arn
        assert ddb.list_tables()["TableNames"] == [s.table]


def test_a_firehose_stream_that_is_still_being_deleted_is_waited_out_and_created_again():
    with mock_aws():
        fh, s = boto3.client("firehose"), Settings(firehose_role_arn="arn:aws:iam::111122223333:role/given")
        create_firehose(fh, s, "111122223333", poll_s=0)        # the stream of the run before the clean-up
        real, state = fh.describe_delivery_stream, {"deleting": 2, "gone": False}

        def describe(**kw):
            """DELETING twice, then gone (the old stream really goes at that moment), then whatever is there."""
            if state["deleting"]:
                state["deleting"] -= 1
                return {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}}
            if not state["gone"]:
                state["gone"] = True
                fh.delete_delivery_stream(DeliveryStreamName=s.delivery_stream)
                raise fh.exceptions.ResourceNotFoundException(
                    {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeDeliveryStream")
            return real(**kw)
        fh.describe_delivery_stream = describe
        create_firehose(fh, s, "111122223333", poll_s=0)
        assert state["gone"] and fh.list_delivery_streams()["DeliveryStreamNames"] == [s.delivery_stream]


def test_a_firehose_stream_that_failed_to_create_is_an_error():
    with mock_aws():
        fh = boto3.client("firehose")
        fh.describe_delivery_stream = lambda **kw: {"DeliveryStreamDescription": {
            "DeliveryStreamStatus": "CREATING_FAILED"}}
        with pytest.raises(RuntimeError):
            create_firehose(fh, Settings(), "111122223333", poll_s=0)
