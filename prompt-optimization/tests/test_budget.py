"""The spend cap: GEPA's metric-call budget, the reflection cap, the money cap, and the hard refusal."""

import json

import pytest

from main import run
from main.models import Budget, BudgetExceeded


def test_reserve_refuses_past_each_cap():
    b = Budget(max_reflection_calls=1, max_task_calls=2, max_test_calls=2, max_cad=1.0)
    b.reserve("reflect")
    with pytest.raises(BudgetExceeded):
        b.reserve("reflect")
    # A refused call of the search cuts it: no further task call of the search is made (the test runs still are).
    with pytest.raises(BudgetExceeded, match="the search was cut"):
        b.reserve("task")
    b = Budget(max_reflection_calls=1, max_task_calls=2, max_test_calls=2, max_cad=1.0)
    b.reserve("task")
    b.reserve("task")
    with pytest.raises(BudgetExceeded):
        b.reserve("task")
    # The test runs have their own allowance: a search stopped by its cap can still be tested.
    b.reserve("test-before")
    b.reserve("test-after")
    with pytest.raises(BudgetExceeded):
        b.reserve("test-after")
    assert b.calls == {"task": 2, "test-before": 1, "test-after": 1}
    b2 = Budget(max_cad=0.001)
    b2.reserve("task")
    b2.settle("task", "qwen3.8-27b", 10_000, 0)   # 10,000 tokens at 0.10 CAD per million = 0.001 CAD
    with pytest.raises(BudgetExceeded):
        b2.reserve("task")


def _spend(tmp_path, *args):
    out = run.main(["--runs", "1", "--out", str(tmp_path), *args])
    return json.loads((out / "spend.json").read_text())


def test_reflection_cap_stops_the_search(tmp_path):
    spend = _spend(tmp_path, "--max-reflection-calls", "1")
    assert spend["calls"]["reflect"] == 1


def test_metric_call_budget_bounds_task_calls(tmp_path):
    spend = _spend(tmp_path, "--max-metric-calls", "30", "--max-reflection-calls", "50")
    # GEPA checks its budget between iterations, so one iteration (3 train + 6 validation calls) may pass it.
    assert spend["calls"]["task"] <= 30 + 9


def test_money_cap_stops_the_search_and_leaves_room_for_the_test(tmp_path):
    # A full stub run spends about 0.004 CAD (stub token estimates); this cap stops the search early.
    spend = _spend(tmp_path, "--max-cad", "0.003", "--max-reflection-calls", "50")
    assert spend["calls"].get("reflect", 0) < 50
    assert spend["calls"]["test-before"] == spend["calls"]["test-after"] == 6
    assert sum(spend["cad"].values()) < 0.003


def test_calls_in_flight_are_held_against_the_money_cap():
    # One task call on qwen3.6-35b-a3b can cost at most 1,000 bytes x 0.05 + 4,000 tokens x 0.10 per million
    # = 0.00045 CAD. A cap of 0.001 has room for two such calls in flight, not three.
    b = Budget(max_cad=0.001)
    first = b.reserve("test-a", "qwen3.6-35b-a3b", 1_000)
    b.reserve("test-a", "qwen3.6-35b-a3b", 1_000)
    assert first == pytest.approx(0.00045) and b.held == pytest.approx(0.0009)
    with pytest.raises(BudgetExceeded, match="held for calls in flight"):
        b.reserve("test-a", "qwen3.6-35b-a3b", 1_000)
    # A settled call gives its hold back and is charged what it used: there is room again.
    b.settle("test-a", "qwen3.6-35b-a3b", 250, 10, first)
    assert b.held == pytest.approx(0.00045) and b.total_cad() == pytest.approx(0.0000135)
    third = b.reserve("test-a", "qwen3.6-35b-a3b", 1_000)
    # A failed call gives its hold back too.
    b.fail("test-a", OSError("429"), third)
    assert b.held == pytest.approx(0.00045)
