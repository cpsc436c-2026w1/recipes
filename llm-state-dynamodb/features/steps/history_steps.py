"""Steps for ChatHistory (main/history.py), against moto."""

from __future__ import annotations

import boto3
from behave import given, then, when

from config import Settings
from history import ChatHistory, NotYourConversation
from tables import ensure_tables

DAY = 86400


def texts(csv: str) -> list[str]:
    return [t.strip() for t in csv.split(",") if t.strip()]


def table(context, kind: str):
    s = Settings()
    name = {"conversations": s.conversations_table, "history": s.history_table}[kind]
    return boto3.resource("dynamodb", region_name=s.region).Table(name)


def attempt(context, fn):
    context.error, context.result = None, None
    try:
        context.result = fn()
    except Exception as e:  # noqa: BLE001  the Then steps say which error was expected
        context.error = e


@given('the tables of the main scenario exist for PREFIX "{prefix}"')
def main_tables(context, prefix):
    assert Settings().prefix == prefix
    resource = boto3.resource("dynamodb", region_name=Settings().region)
    ensure_tables(Settings(), ["conversations", "history"], resource.meta.client)
    context.h = ChatHistory(Settings(), resource, clock=context.clock)
    context.turn_at = {}


@given('"{user}" has a conversation')
@when('"{user}" starts a conversation')
def start(context, user):
    context.started_at = context.clock()
    context.cid = context.h.start(user)


@given('"{user}" has a conversation with the turns "{csv}", one second apart')
def start_with_turns(context, user, csv):
    start(context, user)
    for text in texts(csv):
        context.clock.advance(1)
        context.turn_at[text] = context.h.append(user, context.cid, "user", text)


@when('{n:d} days pass')
@when('{n:d} day passes')
def days_pass(context, n):
    context.clock.advance(n * DAY)


@when('"{user}" adds the turn "{text}"')
def add_turn(context, user, text):
    item = table(context, "conversations").get_item(Key={"conversation_id": context.cid}).get("Item") or {}
    context.expiry_before = item.get("expires_at")

    def go():
        context.turn_at[text] = context.h.append(user, context.cid, "user", text)
    attempt(context, go)


@when('"{user}" asks for her newest {n:d} turns')
def read_own(context, user, n):
    attempt(context, lambda: context.h.recent(user, context.cid, n=n))


@when('"{user}" asks for the newest {n:d} turns of that conversation')
def read_other(context, user, n):
    read_own(context, user, n)


@when('"{user}" asks for the newest {n:d} turns of the conversation "{cid}"')
def read_named(context, user, n, cid):
    context.cid = cid
    read_own(context, user, n)


@given('the turn "{text}" expired one second ago')
@given('the turns "{text}" expired one second ago')
def expire(context, text):
    for t in texts(text):
        table(context, "history").update_item(
            Key={"conversation_id": context.cid, "at": context.turn_at[t]},
            UpdateExpression="SET expires_at = :t", ExpressionAttributeValues={":t": int(context.clock()) - 1})


@given('DynamoDB cancels the next transaction with the reasons "{csv}"')
def cancel_next(context, csv):
    reasons = [{"Code": c} for c in texts(csv)]
    context.calls.answer("TransactWriteItems", error="TransactionCanceledException", times=1,
                         extra={"CancellationReasons": reasons})


@then('the conversation item has the owner "{user}"')
def owner_is(context, user):
    item = table(context, "conversations").get_item(Key={"conversation_id": context.cid})["Item"]
    assert item["owner"] == user, item


@then('the conversation expires {n:d} days from now')
def conversation_expires(context, n):
    item = table(context, "conversations").get_item(Key={"conversation_id": context.cid})["Item"]
    assert int(item["expires_at"]) == int(context.clock() + n * DAY), (int(item["expires_at"]), context.clock())


@then("the conversation's expiry did not change")
def conversation_unchanged(context):
    item = table(context, "conversations").get_item(Key={"conversation_id": context.cid})["Item"]
    assert item["expires_at"] == context.expiry_before, (item, context.expiry_before)


@then('the turn "{text}" expires {n:d} days from now')
def turn_expires(context, text, n):
    assert context.error is None, context.error
    item = table(context, "history").get_item(Key={"conversation_id": context.cid,
                                                   "at": context.turn_at[text]})["Item"]
    assert int(item["expires_at"]) == int(context.clock() + n * DAY), item


@then('the turns are "{csv}"')
def turns_are(context, csv):
    assert context.error is None, repr(context.error)
    assert [t["text"] for t in context.result] == texts(csv), context.result


@then('the turns are "{csv}" in any order')
def turns_any_order(context, csv):
    assert context.error is None, repr(context.error)
    assert sorted(t["text"] for t in context.result) == sorted(texts(csv)), context.result


@then('the call is refused with NotYourConversation')
def refused(context):
    assert isinstance(context.error, NotYourConversation), repr(context.error)


@then('the call is not refused with NotYourConversation')
def not_refused(context):
    assert not isinstance(context.error, NotYourConversation), repr(context.error)


@then('the call fails with TransactionCanceledException')
def cancelled(context):
    code = getattr(context.error, "response", {}).get("Error", {}).get("Code")
    assert code == "TransactionCanceledException", repr(context.error)


@then('the refusal names only the conversation id')
def refusal_text(context):
    assert str(context.error) == context.cid, str(context.error)


@then('no turn was returned')
def nothing_returned(context):
    assert context.result is None, context.result


@then('the turns of the conversation were not queried')
def not_queried(context):
    assert context.calls.of("Query") == [], context.calls.of("Query")


@then('the history table holds only the turns "{csv}"')
def history_holds(context, csv):
    items = table(context, "history").scan()["Items"]
    assert sorted(i["text"] for i in items) == sorted(texts(csv)), items


@then('the history table still stores {n:d} turns')
def history_count(context, n):
    assert len(table(context, "history").scan()["Items"]) == n


@then('the turns were read in {n:d} Query pages')
def query_pages(context, n):
    assert len(context.calls.of("Query")) == n, len(context.calls.of("Query"))


@given('"{user}" has a conversation with turns of {sizes} characters, one second apart')
def start_with_sizes(context, user, sizes):
    start(context, user)
    for k, size in enumerate(int(x) for x in sizes.split(",")):
        context.clock.advance(1)
        context.h.append(user, context.cid, "user", str(k % 10) * size)


@then('the turns returned have {sizes} characters')
def returned_sizes(context, sizes):
    assert context.error is None, repr(context.error)
    assert [len(t["text"]) for t in context.result] == [int(x) for x in sizes.split(",")], [
        len(t["text"]) for t in context.result]
