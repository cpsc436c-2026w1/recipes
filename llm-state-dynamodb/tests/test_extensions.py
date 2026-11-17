"""ext-answer-cache: hit, miss, version bump, no sharing of personal answers. ext-traces: calls in order."""

from __future__ import annotations

import pytest

from answer_cache import AnswerCache, answer_with_cache, cache_key
from trace_store import Tracer, calls_of


class Fakes:
    def __init__(self):
        self.retrievals = 0
        self.model_calls = 0

    def retrieve(self, question, user_id):
        self.retrievals += 1
        if "order" in question.lower():
            return [{"id": f"order-of-{user_id}", "owner": user_id, "text": f"{user_id}'s order shipped"}]
        return [{"id": "policy", "owner": "", "text": "30 days"}]

    def generate(self, question, docs):
        self.model_calls += 1
        return " ".join(d["text"] for d in docs)


@pytest.fixture
def cache(s, tables, clock):
    return AnswerCache(s.cache_table, tables, clock=clock)


def ask(cache, f, user, q, version="idx-1"):
    return answer_with_cache(q, user_id=user, index_version=version, cache=cache,
                             retrieve=f.retrieve, generate=f.generate)


def test_miss_then_hit_skips_retrieval_and_model(cache):
    f = Fakes()
    _, hit1 = ask(cache, f, "user-ana", "What is the return policy?")
    ans, hit2 = ask(cache, f, "user-ben", "what is the   return policy")
    assert (hit1, hit2) == (False, True)
    assert (f.retrievals, f.model_calls) == (1, 1)
    assert ans["reply"] == "30 days"


def test_version_bump_misses(cache):
    f = Fakes()
    ask(cache, f, "user-ana", "What is the return policy?", "idx-1")
    _, hit = ask(cache, f, "user-ana", "What is the return policy?", "idx-2")
    assert hit is False and f.retrievals == 2
    assert cache_key("q", "idx-1", "shared") != cache_key("q", "idx-2", "shared")


def test_personal_answer_never_shared(cache):
    f = Fakes()
    a, _ = ask(cache, f, "user-ana", "Where is my order?")
    b, hit = ask(cache, f, "user-ben", "Where is my order?")
    assert hit is False and "user-ana" not in b["reply"]
    again, hit_again = ask(cache, f, "user-ana", "Where is my order?")
    assert hit_again is True and again["reply"] == a["reply"]


def test_private_source_keeps_answer_per_user_without_personal_words(cache):
    f = Fakes()
    a, _ = ask(cache, f, "user-ana", "Order status")       # no "my", but the retrieved document is ana's
    assert a["scope"] == "user"
    b, hit = ask(cache, f, "user-ben", "Order status")
    assert hit is False and "user-ana" not in b["reply"]


def test_expired_entry_is_a_miss(cache, clock):
    f = Fakes()
    ask(cache, f, "user-ana", "What is the return policy?")
    clock.advance(24 * 3600 + 1)
    _, hit = ask(cache, f, "user-ana", "What is the return policy?")
    assert hit is False


def test_trace_calls_in_order_by_request(s, tables, clock):
    t = Tracer(s.traces_table, tables, user_id="user-ana", clock=clock)
    other = Tracer(s.traces_table, tables, user_id="user-ben", clock=clock)
    with t.call("model", "plan") as c:
        c.tokens_in, c.tokens_out = 100, 10
    with other.call("model", "plan"):
        pass
    with pytest.raises(TimeoutError):
        with t.call("tool", "search"):
            raise TimeoutError("slow")
    with t.call("model", "reply"):
        pass
    got = calls_of(s.traces_table, tables, t.request_id)
    assert [(int(i["seq"]), i["kind"], i["name"], i["status"]) for i in got] == [
        (1, "model", "plan", "ok"), (2, "tool", "search", "error"), (3, "model", "reply", "ok")]
    assert int(got[0]["tokens_in"]) == 100
    assert all("user-ana" not in str(i.values()) for i in got)  # user id stored only as a hash
    assert int(got[0]["expires_at"]) == int(clock()) + 7 * 86400


def test_trace_write_failure_does_not_fail_the_call(s, tables, clock, capsys):
    t = Tracer("missing-table", tables, user_id="u", clock=clock)
    with t.call("model", "plan"):
        pass
    assert "trace write failed" in capsys.readouterr().out
