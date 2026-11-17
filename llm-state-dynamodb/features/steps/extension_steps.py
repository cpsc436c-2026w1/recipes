"""Steps for the two extensions: the answer cache and the traces, against moto."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import boto3
from behave import given, then, when
from answer_cache import AnswerCache, answer_with_cache, cache_key
from trace_store import Tracer, calls_of

from config import Settings
from tables import ensure_tables

DAY = 86400


def resource():
    return boto3.resource("dynamodb", region_name=Settings().region)


# ---- the answer cache ----

class CountingRetriever:
    """Public documents for everyone; each user's own order only for that user (the retriever's own filter)."""

    def __init__(self, public: str):
        self.docs = [{"id": "policy", "owner": "", "text": public},
                     {"id": "order-1042", "owner": "user-ana", "text": "Order 1042 shipped."},
                     {"id": "order-2077", "owner": "user-ben", "text": "Order 2077 is waiting for stock."}]
        self.retrievals = 0
        self.model_calls = 0

    def retrieve(self, question, user_id):
        self.retrievals += 1
        if "order" in question.lower():
            return [d for d in self.docs if d["owner"] == user_id]
        return [d for d in self.docs if d["owner"] == ""]

    def generate(self, question, docs):
        self.model_calls += 1
        return " ".join(d["text"] for d in docs)


@given('the answer cache table exists')
def cache_table(context):
    ensure_tables(Settings(), ["cache"], resource().meta.client)
    context.cache = AnswerCache(Settings().cache_table, resource(), clock=context.clock)
    context.asked = []   # (user, answer, hit)


@given('a retriever with the public document "{text}" and one order per user (user-ana: order 1042, user-ben: order 2077)')
def retriever(context, text):
    context.fake = CountingRetriever(text)


@given('an answer to "{question}" is stored under the shared key for "{version}"')
def stored_shared(context, question, version):
    context.cache.put(cache_key(question, version, "shared"),
                      {"question": question, "reply": "Order 1042 shipped.", "sources": [], "scope": "shared"})


@when('"{user}" asks "{question}" with index version "{version}"')
def ask(context, user, question, version):
    answer, hit = answer_with_cache(question, user_id=user, index_version=version, cache=context.cache,
                                    retrieve=context.fake.retrieve, generate=context.fake.generate)
    context.asked.append((user, answer, hit))


@when('{seconds:d} seconds pass')
def seconds_pass(context, seconds):
    context.clock.advance(seconds)


@then('the answers were "{csv}"')
def answers_were(context, csv):
    got = ["hit" if hit else "miss" for _, _, hit in context.asked]
    assert got == [x.strip() for x in csv.split(",")], got


@then('the retriever was called {r:d} time and the model {m:d} time')
@then('the retriever was called {r:d} times and the model {m:d} times')
def counts(context, r, m):
    assert (context.fake.retrievals, context.fake.model_calls) == (r, m), (context.fake.retrievals,
                                                                          context.fake.model_calls)


@then('the last answer is "{text}"')
def last_answer(context, text):
    assert context.asked[-1][1]["reply"] == text, context.asked[-1]


@then('no answer to "{user}" holds "{text}"')
def no_leak(context, user, text):
    for who, answer, _ in context.asked:
        if who == user:
            assert text not in json.dumps(answer), answer


@then('the cache table still stores {n:d} entry')
def cache_count(context, n):
    assert len(resource().Table(Settings().cache_table).scan()["Items"]) == n


@then('no item of the cache table holds "{user}"')
def cache_no_user(context, user):
    items = resource().Table(Settings().cache_table).scan()["Items"]
    assert items
    for item in items:
        assert user not in json.dumps(item, default=str), item


@then('the last printed line is "{text}"')
def last_line(context, text):
    assert context.printed.rstrip("\n").splitlines()[-1] == text, context.printed


@then('the cache counted {hits:d} hit and {misses:d} misses')
def cache_counts(context, hits, misses):
    assert (context.cache.hits, context.cache.misses) == (hits, misses), (context.cache.hits, context.cache.misses)


# ---- traces ----

@given('the traces table exists')
def traces_table(context):
    ensure_tables(Settings(), ["traces"], resource().meta.client)
    context.tracers = {}
    context.out = io.StringIO()


@given('"{user}" sends a request')
def a_request(context, user):
    context.tracers[user] = Tracer(Settings().traces_table, resource(), user_id=user, conversation_id="c-1",
                                   clock=context.clock)


@given('"{a}" and "{b}" send requests at the same time')
def two_requests(context, a, b):
    a_request(context, a)
    a_request(context, b)


@given('AWS refuses PutItem on the traces table with "{code}"')
def refuse_put(context, code):
    name = Settings().traces_table
    context.calls.answer("PutItem", when=lambda p: p.get("TableName") == name, error=code)


@given('DynamoDB returns at most {n:d} items per Query page')
def small_pages(context, n):
    # DynamoDB ends a page at 1 MB; a smaller page shows the same paging with a few items.
    context.calls.answer("Query", params=lambda p: {**p, "Limit": n})


def make_calls(context, user, csv):
    tracer = context.tracers[user]
    context.error = None
    with redirect_stdout(context.out):
        for kind_name in csv.split(","):
            kind, name = kind_name.split()
            with tracer.call(kind, name) as c:
                c.tokens_in, c.tokens_out = 10, 2
                context.clock.advance(1)


@when("{user}'s request makes the calls \"{csv}\"")
def request_makes_calls(context, user, csv):
    make_calls(context, user, csv)


@when("{user}'s request makes a tool call \"{name}\" that raises TimeoutError \"{message}\"")
def call_raises(context, user, name, message):
    try:
        with context.tracers[user].call("tool", name):
            raise TimeoutError(message)
    except TimeoutError as e:
        context.error = e


@when("{user}'s request makes a tool call \"{name}\" that raises TimeoutError with {n:d} characters")
def call_raises_long(context, user, name, n):
    call_raises(context, user, name, "x" * n)


@when("the calls of {user}'s request are read back")
def read_back(context, user):
    context.read = calls_of(Settings().traces_table, resource(), context.tracers[user].request_id)


@then('they are "{csv}"')
def they_are(context, csv):
    got = [f"{int(i['seq'])} {i['kind']} {i['name']} {i['status']}" for i in context.read]
    assert got == [x.strip() for x in csv.split(",")], got


@then('they are numbered {a:d} to {b:d}')
def numbered(context, a, b):
    assert [int(i["seq"]) for i in context.read] == list(range(a, b + 1)), context.read


@then('the caller got the TimeoutError')
def caller_got(context):
    assert isinstance(context.error, TimeoutError), context.error


@then('the error of call {n:d} is "{text}"')
def error_is(context, n, text):
    assert context.read[n - 1]["error"] == text, context.read[n - 1]


@then('the error of call {n:d} has {length:d} characters')
def error_length(context, n, length):
    assert len(context.read[n - 1]["error"]) == length, len(context.read[n - 1]["error"])


@then('the call returned normally')
def returned(context):
    assert context.error is None, context.error


@then('the request printed "{text}"')
def out_holds(context, text):
    assert text in context.out.getvalue(), context.out.getvalue()


@then('no trace item holds "{text}"')
def no_trace_holds(context, text):
    items = resource().Table(Settings().traces_table).scan()["Items"]
    assert items and all(text not in json.dumps(i, default=str) for i in items), items


@then('every trace item has the fields "{csv}" and no other')
def trace_fields(context, csv):
    want = sorted(x.strip() for x in csv.split(","))
    for item in resource().Table(Settings().traces_table).scan()["Items"]:
        assert sorted(item) == want, sorted(item)


@then('trace item {n:d} expires {days:d} days from now')
def trace_expires(context, n, days):
    items = sorted(resource().Table(Settings().traces_table).scan()["Items"], key=lambda i: int(i["seq"]))
    assert int(items[n - 1]["expires_at"]) == int(context.clock() + days * DAY), items[n - 1]


@then('the printed lines after the request id are:')
def lines_after_request(context):
    lines = context.printed.rstrip("\n").splitlines()
    i = next(k for k, line in enumerate(lines) if line.startswith("request "))
    assert lines[i + 1:] == context.text.splitlines(), "\n".join(lines)
