"""Main scenario: read your own history, a cross-user read is refused, expired turns are filtered."""

from __future__ import annotations

import pytest

from history import ChatHistory, NotYourConversation


@pytest.fixture
def h(s, tables, clock):
    return ChatHistory(s, tables, clock=clock)


def test_tables_have_ttl_on_expires_at(s, tables):
    c = tables.meta.client
    for name in s.all_tables():
        ttl = c.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
        assert ttl["TimeToLiveStatus"] == "ENABLED" and ttl["AttributeName"] == "expires_at"
        assert c.describe_table(TableName=name)["Table"]["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"


def test_read_own_history_newest_first(h, clock):
    cid = h.start("user-ana")
    for i in range(5):
        clock.advance(1)
        h.append("user-ana", cid, "user", f"turn {i}")
    got = h.recent("user-ana", cid, n=3)
    assert [t["text"] for t in got] == ["turn 4", "turn 3", "turn 2"]


def test_two_turns_in_the_same_millisecond_both_kept(h):
    cid = h.start("user-ana")
    h.append("user-ana", cid, "user", "a")
    h.append("user-ana", cid, "assistant", "b")  # same clock value
    assert sorted(t["text"] for t in h.recent("user-ana", cid)) == ["a", "b"]


def test_cross_user_read_is_refused(h):
    cid = h.start("user-ana")
    h.append("user-ana", cid, "user", "my address is 12 Oak St")
    with pytest.raises(NotYourConversation):
        h.recent("user-ben", cid)


def test_cross_user_write_is_refused_and_writes_nothing(h, s, tables):
    cid = h.start("user-ana")
    with pytest.raises(NotYourConversation):
        h.append("user-ben", cid, "user", "injected turn")
    items = tables.Table(s.history_table).scan()["Items"]
    assert items == []  # the transaction wrote neither the turn nor the conversation update


def test_unknown_conversation_looks_like_a_foreign_one(h):
    with pytest.raises(NotYourConversation):
        h.recent("user-ana", "no-such-conversation")


def test_expired_turns_filtered_before_dynamodb_deletes_them(h, s, tables, clock):
    cid = h.start("user-ana")
    h.append("user-ana", cid, "user", "old turn")
    # moto, like DynamoDB in the first hours or days after expiry, still holds the item: the reader must filter.
    old = tables.Table(s.history_table).scan()["Items"][0]
    tables.Table(s.history_table).update_item(Key={"conversation_id": cid, "at": old["at"]},
                                              UpdateExpression="SET expires_at = :t",
                                              ExpressionAttributeValues={":t": int(clock()) - 1})
    clock.advance(1)
    h.append("user-ana", cid, "user", "new turn")
    assert len(tables.Table(s.history_table).scan()["Items"]) == 2  # still stored
    assert [t["text"] for t in h.recent("user-ana", cid)] == ["new turn"]


def test_short_pages_after_filter_keep_paging(h, s, tables, clock):
    cid = h.start("user-ana")
    h.append("user-ana", cid, "user", "kept")
    for i in range(4):
        clock.advance(1)
        at = h.append("user-ana", cid, "user", f"expired {i}")
        tables.Table(s.history_table).update_item(Key={"conversation_id": cid, "at": at},
                                                  UpdateExpression="SET expires_at = :t",
                                                  ExpressionAttributeValues={":t": 0})
    # The newest 2 items read are both expired; Limit=2 alone would return nothing.
    assert [t["text"] for t in h.recent("user-ana", cid, n=2)] == ["kept"]


def test_expired_conversation_is_refused(h, clock):
    cid = h.start("user-ana")
    clock.advance(31 * 86400)
    with pytest.raises(NotYourConversation):
        h.recent("user-ana", cid)


def test_a_cancelled_write_that_is_not_an_owner_refusal_is_not_reported_as_one(h):
    """Two writes to one conversation at the same moment: DynamoDB cancels one with TransactionConflict."""
    cid = h.start("user-ana")

    def conflict(**kw):
        raise h.client.exceptions.TransactionCanceledException(
            {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
             "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}]}, "TransactWriteItems")

    real = h.client.transact_write_items
    h.client.transact_write_items = conflict
    try:
        with pytest.raises(h.client.exceptions.TransactionCanceledException):
            h.append("user-ana", cid, "user", "hello")  # the owner: a conflict, not "not your conversation"
    finally:
        h.client.transact_write_items = real
    with pytest.raises(NotYourConversation):
        h.append("user-ben", cid, "user", "hello")  # a failed owner condition is still a refusal
