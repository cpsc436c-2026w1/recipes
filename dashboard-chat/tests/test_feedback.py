"""A user's verdict on an answer (ext-feedback): on the trace item, only if it exists, only up or down."""

import boto3
import pytest
from moto import mock_aws

from main.chat import answer
from main.config import Settings
from main.infra import ensure_table
from main.traces import DynamoTraces, LocalTraces


def test_local_verdict_is_written_on_the_trace_line_and_replaced_by_a_later_one(engine, stub, tmp_path):
    from feedback import LocalFeedback, NoSuchTrace, report

    path = tmp_path / "traces.jsonl"
    traces = LocalTraces(path)
    first = answer("How many orders are there?", user_id="c-0007", model=stub, engine=engine, traces=traces)
    second = answer("Delete all the orders", user_id="c-0007", model=stub, engine=engine, traces=traces)
    feedback = LocalFeedback(path)
    feedback.mark(first.request_id, "down", "c-0007")
    item = feedback.mark(first.request_id, "up", "c-0007")
    assert item["feedback"] == "up" and len(feedback.items()) == 2
    feedback.mark(second.request_id, "down", "c-0007")
    summary = report(feedback.items())
    assert (summary["answers"], summary["up"], summary["down"], summary["not_marked"]) == (2, 1, 1, 0)
    assert [row["question"] for row in summary["marked_down"]] == ["Delete all the orders"]
    before = path.read_text()
    with pytest.raises(ValueError, match="up, down"):
        feedback.mark(first.request_id, "maybe", "c-0007")
    with pytest.raises(NoSuchTrace):
        feedback.mark("no-such-request", "up", "c-0007")
    with pytest.raises(NoSuchTrace):  # another user may not mark it
        feedback.mark(first.request_id, "down", "c-0012")
    assert path.read_text() == before  # a refused verdict writes nothing


@mock_aws
def test_dynamodb_verdict_updates_the_item_and_never_creates_one(engine, stub):
    from feedback import DynamoFeedback, NoSuchTrace

    s = Settings()
    ensure_table(s.traces_table, s.region, key="request_id")
    traces = DynamoTraces(s.traces_table, s.region)
    a = answer("How many orders are there?", user_id="c-0007", model=stub, engine=engine, traces=traces)
    before = traces.get(a.request_id)
    feedback = DynamoFeedback(s.traces_table, s.region)
    feedback.mark(a.request_id, "down", "c-0007")
    after = traces.get(a.request_id)
    assert after["feedback"] == "down" and after["feedback_at"]
    assert {k: v for k, v in after.items() if not k.startswith("feedback")} == before  # nothing else changed
    with pytest.raises(NoSuchTrace):
        feedback.mark("no-such-request", "up", "c-0007")
    with pytest.raises(NoSuchTrace, match="of this user"):  # another user's verdict changes nothing
        feedback.mark(a.request_id, "up", "c-0012")
    with pytest.raises(ValueError):
        feedback.mark(a.request_id, "maybe", "c-0007")
    table = boto3.resource("dynamodb", region_name=s.region).Table(s.traces_table)
    assert table.scan()["Count"] == 1 and traces.get(a.request_id)["feedback"] == "down"
