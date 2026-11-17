"""A run right after a clean-up, a Firehose stream that does not start, a bucket name that is taken.

moto plays AWS; the answers moto does not give (DELETING, "in use", a failed start) are put in by hand.
"""
import pytest

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


# ---------- a run right after a clean-up: names that are still "in use" by something on its way out

def test_a_kinesis_stream_that_is_still_being_deleted_is_waited_out_and_created_again(aws):
    kinesis = aws.client("kinesis")
    refuse(kinesis, "create_stream", "ResourceInUseException")
    real, answers = kinesis.describe_stream_summary, ["DELETING", "DELETING"]
    kinesis.describe_stream_summary = lambda **kw: (
        {"StreamDescriptionSummary": {"StreamStatus": answers.pop(0)}} if answers else real(**kw))
    lakehouse.create_stream(kinesis, "c1-updates", sleep=lambda s: None)
    assert answers == [] and kinesis.list_streams()["StreamNames"] == ["c1-updates"]


def test_a_table_that_is_still_being_deleted_is_waited_out_and_created_again(aws):
    ddb = aws.client("dynamodb")
    refuse(ddb, "create_table", "ResourceInUseException")
    real, answers = ddb.describe_table, ["DELETING", "DELETING"]
    ddb.describe_table = lambda **kw: {"Table": {"TableStatus": answers.pop(0)}} if answers else real(**kw)
    operational.create_table(ddb, "c1-updates", sleep=lambda s: None)
    assert answers == [] and ddb.list_tables()["TableNames"] == ["c1-updates"]


def test_a_firehose_stream_that_is_still_being_deleted_is_waited_out_and_created_again(aws):
    n = names("c1", account=ACCOUNT)
    made = lakehouse.setup(aws, n, ACCOUNT, sleep=lambda s: None)
    fh = aws.client("firehose")
    real, state = fh.describe_delivery_stream, {"deleting": 2, "gone": False}

    def describe(**kw):
        """DELETING twice, then gone (the old stream really goes at that moment), then whatever is there."""
        if state["deleting"]:
            state["deleting"] -= 1
            return {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}}
        if not state["gone"]:
            state["gone"] = True
            fh.delete_delivery_stream(DeliveryStreamName=n["firehose"])
            raise fh.exceptions.ResourceNotFoundException({"Error": {"Code": "ResourceNotFoundException"}}, "Describe")
        return real(**kw)
    fh.describe_delivery_stream = describe
    lakehouse.create_firehose(fh, n, made["stream_arn"], made["firehose_role"], sleep=lambda s: None)
    assert state["gone"] and fh.list_delivery_streams()["DeliveryStreamNames"] == [n["firehose"]]


def test_a_firehose_stream_that_failed_to_start_is_an_error_not_an_endless_wait(aws):
    n = names("c1", account=ACCOUNT)
    made = lakehouse.setup(aws, n, ACCOUNT, sleep=lambda s: None)
    fh = aws.client("firehose")
    fh.describe_delivery_stream = lambda **kw: {"DeliveryStreamDescription": {
        "DeliveryStreamStatus": "CREATING_FAILED"}}
    with pytest.raises(RuntimeError, match="CREATING_FAILED"):
        lakehouse.create_firehose(fh, n, made["stream_arn"], made["firehose_role"], sleep=lambda s: None)


def test_a_firehose_stream_that_never_becomes_active_times_out(aws):
    n = names("c1", account=ACCOUNT)
    made = lakehouse.setup(aws, n, ACCOUNT, sleep=lambda s: None)
    fh = aws.client("firehose")
    fh.describe_delivery_stream = lambda **kw: {"DeliveryStreamDescription": {"DeliveryStreamStatus": "CREATING"}}
    with pytest.raises(TimeoutError):
        lakehouse.create_firehose(fh, n, made["stream_arn"], made["firehose_role"], sleep=lambda s: None,
                                  wait_tries=3)


def test_a_bucket_name_owned_by_another_account_stops_the_run(aws):
    from skeleton.aws import ensure_bucket

    s3 = aws.client("s3")
    ensure_bucket(s3, "c1-lake-123456789012")
    ensure_bucket(s3, "c1-lake-123456789012")                  # ours already: fine
    refuse(s3, "create_bucket", "BucketAlreadyExists")
    with pytest.raises(s3.exceptions.ClientError):
        ensure_bucket(s3, "c1-lake-123456789012")
