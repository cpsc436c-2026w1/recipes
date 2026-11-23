"""run_log.py: ensure_run_log creates the table once, borrows one that is there, refuses other keys."""
import pytest

import run_log
import tags


def test_creates_once_and_records_it(aws):
    assert run_log.ensure_run_log(aws) == "recipe-test-runs"
    table = aws.client("dynamodb").describe_table(TableName="recipe-test-runs")["Table"]
    assert table["KeySchema"] == run_log.KEY_SCHEMA
    assert table["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"
    run_log.ensure_run_log(aws)
    assert [r["state"] for r in tags.ledger_rows()] == ["created"]


def test_a_table_that_was_there_is_borrowed(aws):
    aws.client("dynamodb").create_table(
        TableName="recipe-test-runs", KeySchema=run_log.KEY_SCHEMA, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "job", "AttributeType": "S"},
                              {"AttributeName": "run_id", "AttributeType": "S"}])
    run_log.ensure_run_log(aws)
    assert tags.recorded("dynamodb-table", "recipe-test-runs") == "borrowed"


def test_other_keys_stop(aws):
    aws.client("dynamodb").create_table(
        TableName="recipe-test-runs", KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST", AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])
    with pytest.raises(RuntimeError, match="key schema"):
        run_log.ensure_run_log(aws)
