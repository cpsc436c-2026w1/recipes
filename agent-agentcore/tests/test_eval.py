"""The Inspect eval, offline: the task runs on the stub model, and the scorer in code is not a rubber stamp."""
import copy

import pytest
from inspect_ai import eval as inspect_eval

from evals import scoring
from evals.task import member_requests, one_request, samples


def test_the_stub_eval_scores_every_request_as_expected(tmp_path):
    logs = inspect_eval(member_requests(), log_dir=str(tmp_path), display="none")
    log = logs[0]
    assert log.status == "success", log.error
    scores = {s.name: s.metrics for s in log.results.scores}
    assert scores["outcome"]["accuracy"].value == 1.0
    assert len(log.samples) == len(samples("stub")) >= 8
    failed = [(s.id, s.scores["outcome"].explanation) for s in log.samples if s.scores["outcome"].value != 1]
    assert failed == []


def test_the_stub_eval_makes_no_network_call(tmp_path, monkeypatch):
    import socket

    def refuse(self, address):
        raise AssertionError(f"a network call to {address}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert inspect_eval(member_requests(), log_dir=str(tmp_path), display="none")[0].status == "success"


def test_the_live_eval_leaves_out_the_samples_that_name_a_stub_behaviour():
    live = samples("gateway")
    assert 4 <= len(live) < len(samples("stub")) and all("stub" not in s.metadata for s in live)


def test_the_live_eval_asks_for_the_token_before_it_starts():
    with pytest.raises(SystemExit) as stop:
        member_requests(backend="gateway")
    assert "Set KB_MCP_TOKEN in .env" in str(stop.value)


ROW = {"id": "x", "input": "Which is shorter, Dune or Emma? Place a hold on the shorter one for me.",
       "member": "sam", "answer": "y", "expect": {"status": "done", "tools": ["search_books", "place_hold"],
                                                  "holds": ["b02"], "approvals": 1}}


def test_the_scorer_passes_the_run_the_sample_expects(s):
    report, holds = one_request(s, "stub", ROW)
    assert scoring.check(report, holds, ROW["expect"], s) == []


@pytest.mark.parametrize("change, says", [
    (lambda r, h: r.update(model_calls=99), "over the cap"),
    (lambda r, h: r.update(tools_ran=r["tools_ran"] + ["place_hold"]), "writes ran with 1 approvals"),
    (lambda r, h: h.update(alex=["b02"]), "other members"),
    (lambda r, h: h.update(sam=["b01"]), "holds ['b01'], expected ['b02']"),
    (lambda r, h: r.update(status="stopped"), "status stopped"),
    (lambda r, h: r.update(tools_ran=["place_hold"]), "search_books did not run"),
    (lambda r, h: r.update(approvals_asked=[]), "0 approvals asked"),
])
def test_the_scorer_finds_each_kind_of_problem(s, change, says):
    report, holds = one_request(s, "stub", ROW)
    report, holds = copy.deepcopy(report), copy.deepcopy(holds)
    change(report, holds)
    assert any(says in p for p in scoring.check(report, holds, ROW["expect"], s))
