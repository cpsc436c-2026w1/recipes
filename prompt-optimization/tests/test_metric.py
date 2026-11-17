"""The metric names the failure type, and its feedback says what came back."""

import pytest

from main.data import load_dev
from main.metric import check

Q = {q["id"]: q for split in load_dev().values() for q in split}


@pytest.mark.parametrize("qid, sql, kind", [
    ("t01", "SELECT COUNT(*) FROM orders", "pass"),
    ("t01", "SELECT COUNT(*) FROM orders\nThis query answers the question.", "SQL error"),
    ("t01", "SELECT COUNT(*) FROM orders; SELECT 1", "SQL error"),
    ("t01", "DELETE FROM orders", "SQL error"),
    ("t01", "SELECT order_id FROM orders", "SQL error"),            # many rows, not one number
    ("t01", "SELECT COUNT(*) FROM no_such_table", "SQL error"),
    ("t02", "SELECT AVG(CASE WHEN payment = 'cash' THEN 1 ELSE 0 END) FROM orders", "wrong unit"),
    ("t03", "SELECT AVG(epoch(ready_at - placed_at)) FROM orders", "wrong unit"),
    ("t09", "SELECT AVG(price_cad) FROM orders", "wrong value"),
    ("t01", "", "no reply"),
    ("t01", None, "no reply"),
])
def test_failure_types(qid, sql, kind):
    v = check(Q[qid], sql)
    assert v.kind == kind
    assert v.score == (1.0 if kind == "pass" else 0.0)
    assert Q[qid]["question"] in v.feedback
    if kind != "pass":
        assert "FAILURE" in v.feedback and kind in v.feedback


def test_a_delete_does_not_change_the_data():
    check(Q["t01"], "DELETE FROM orders")
    assert check(Q["t01"], "SELECT COUNT(*) FROM orders").kind == "pass"
