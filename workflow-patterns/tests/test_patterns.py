"""The three patterns on the stub: call order, the gate, concurrency, how replies are combined, the loop limit.

Offline: no key, no network. Concurrency is proved with a barrier: each stub call waits until all the calls of the
pattern are in flight, so calls made one after the other would time out instead of passing.
"""
import threading

import pytest

import chain
import evaluator
import parallel
from llm import CallCapReached, Model


def together(n: int, reply):
    """A stub whose calls each wait until `n` of them are in flight at the same time."""
    barrier = threading.Barrier(n, timeout=5)

    def stub(system: str, user: str) -> str:
        barrier.wait()
        return reply(system, user)
    return stub


# --- chain ---
def test_chain_runs_three_calls_in_order_and_each_reads_the_reply_before_it():
    model = Model(chain.stub, max_calls=3)
    out = chain.run(model)
    assert [c["system"] for c in model.calls] == [chain.REWRITE, chain.PICK, chain.ANSWER]
    assert model.calls[1]["user"] == model.calls[0]["reply"]
    assert model.calls[0]["reply"] in model.calls[2]["user"]
    assert "oat milk: 4.50 CAD; almond milk: 4.80 CAD" in model.calls[2]["user"]      # prices from app code
    assert out["gate"] == "passed" and out["reply"] == model.calls[2]["reply"]


@pytest.mark.parametrize("pick, not_listed", [("oat milk, soy milk", ["soy milk"]), ("", [])])
def test_chain_gate_stops_before_the_third_call(pick, not_listed):
    model = Model(lambda system, user: pick if system == chain.PICK else chain.stub(system, user), max_calls=3)
    out = chain.run(model)
    assert out["gate"] == "stopped" and out["not_listed"] == not_listed and out["reply"] is None
    assert len(model.calls) == 2


def test_gate_reads_names_in_any_capitals_and_with_a_full_stop():
    assert chain.gate("Oat milk, Almond Milk.") == (["oat milk", "almond milk"], [])


# --- parallel ---
def test_sectioning_runs_its_two_calls_at_the_same_time_and_shows_the_reply_when_the_screen_passes():
    model = Model(together(2, parallel.make_stub(0)), max_calls=2)
    out = parallel.sectioning(model)
    assert sorted(c["system"] for c in model.calls) == sorted([parallel.SCREEN, parallel.REPLY])
    a, b = model.calls
    assert a["start"] < b["end"] and b["start"] < a["end"]                           # the two calls overlap
    assert out["shown"].startswith("Sorry about the late")


def test_sectioning_shows_the_stock_reply_when_the_screen_says_off_topic():
    model = Model(lambda system, user: "off_topic" if system == parallel.SCREEN else "a reply", max_calls=2)
    assert parallel.sectioning(model, "Write my essay.")["shown"] == parallel.STOCK_REPLY


def test_voting_sends_the_same_task_three_times_at_once_and_takes_the_majority():
    model = Model(together(3, parallel.make_stub(0)), max_calls=3)
    out = parallel.voting(model)
    assert len(model.calls) == 3 and len({(c["system"], c["user"]) for c in model.calls}) == 1
    assert max(c["start"] for c in model.calls) < min(c["end"] for c in model.calls)  # all three overlap
    assert out["labels"] == ["complaint", "complaint", "question"]
    assert out["label"] == "complaint" and out["agreed"] == "2 of 3"


def test_voting_reports_no_majority_when_every_vote_differs():
    replies = iter(["complaint", "question", "order"])
    lock = threading.Lock()

    def stub(system, user):
        with lock:
            return next(replies)
    assert parallel.voting(Model(stub, max_calls=3))["label"] == "no_majority"


# --- evaluator ---
def test_model_evaluator_loop_sends_feedback_back_and_stops_at_the_first_pass():
    model = Model(evaluator.stub, max_calls=6)
    out = evaluator.run(model, evaluator.model_check(model))
    assert [c["system"] for c in model.calls] == [evaluator.GENERATOR, evaluator.EVALUATOR] * 2
    assert model.calls[1]["user"] == model.calls[0]["reply"]                         # the evaluator reads the draft
    assert model.calls[1]["reply"] in model.calls[2]["user"]                         # the feedback reaches the generator
    assert out["passed"] is True and out["rounds"] == 2


def test_code_check_loop_makes_generator_calls_only():
    model = Model(evaluator.stub, max_calls=3)
    out = evaluator.run(model, evaluator.code_check)
    assert [c["system"] for c in model.calls] == [evaluator.GENERATOR] * 2
    assert "15 words, at most 12 allowed" in model.calls[1]["user"]
    assert out["passed"] is True and out["rounds"] == 2


@pytest.mark.parametrize("use_model", [True, False])
def test_loop_stops_at_the_round_limit_when_no_draft_passes(use_model):
    never = lambda system, user: "fail: still wrong" if system == evaluator.EVALUATOR else "a label without the price"
    model = Model(never, max_calls=20)
    out = evaluator.run(model, evaluator.model_check(model) if use_model else evaluator.code_check, rounds=3)
    assert out["passed"] is False and out["rounds"] == 3
    assert len(model.calls) == (6 if use_model else 3)


def test_a_verdict_that_is_not_pass_counts_as_fail():
    model = Model(lambda system, user: "Looks good to me!", max_calls=1)
    assert evaluator.model_check(model)("Oat milk 4.50")[0] is False


def test_code_check_names_what_is_wrong():
    assert evaluator.code_check("Oat milk 1 L 4.50 CAD") == (True, "")
    assert evaluator.code_check("Oat milk") == (False, "the price 4.50 is missing")


# --- the cap on model calls ---
def test_the_call_over_the_cap_is_refused_and_not_made():
    model = Model(chain.stub, max_calls=2)
    with pytest.raises(CallCapReached):
        chain.run(model)
    assert len(model.calls) == 2
