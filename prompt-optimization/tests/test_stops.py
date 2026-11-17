"""A refused or failed model call ends the run and says so; it is never scored as a wrong answer.

GEPA (ext-gepa-direct, raise_on_exception=False) logs an exception from an evaluation and starts the next
iteration; DSPy (main/) scores it 0. Without a stop condition that sees the refusal, the direct run never ends.
"""

import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import ROOT, load_script
from main import run
from main.models import Budget, BudgetExceeded, ModelCallFailed, gateway_chat, make_lm
from main.report import evaluate

# The run with a cap of 40 task-model calls: the seed's 6 validation calls fit, the search runs out part-way.
SMALL_CAP = """
import sys
sys.path.insert(0, "tests")
from conftest import load_script
direct = load_script("ext-gepa-direct/optimize_direct.py")
real = direct.Budget
direct.Budget = lambda **kw: real(**{**kw, "max_task_calls": 40})
direct.main(["--runs", "1", "--out", sys.argv[1]])
"""


def test_stop_is_true_after_a_refused_call_and_names_it():
    b = Budget(max_task_calls=1, max_reflection_calls=5)
    b.reserve("task")
    assert b.stop() is False
    with pytest.raises(BudgetExceeded):
        b.reserve("task")
    assert b.stop() is True
    assert "call refused" in b.stopped_by and "cap of 1" in b.stopped_by


def test_direct_search_ends_when_the_cap_refuses_a_call(tmp_path):
    # A subprocess with a timeout: before the fix this run did not end.
    done = subprocess.run([sys.executable, "-c", SMALL_CAP, str(tmp_path)], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr
    # The search stopped at the cap, and the test runs still ran on their own allowance.
    report = (tmp_path / "report.md").read_text()
    assert "The search ended at: spend cap, call refused (task: 40 calls reached the cap of 40)" in report
    assert "| before |" in report and "| after |" in report
    assert (tmp_path / "instructions_after.txt").read_text().strip()
    assert json.loads((tmp_path / "candidates.json").read_text())
    calls = json.loads((tmp_path / "spend.json").read_text())["calls"]
    assert calls["task"] == 40 and calls["test-before"] == calls["test-after"] == 6


class _Watched(Budget):
    """A Budget that counts the reflection calls made after the first refused task call."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reflect_after_refusal = 0
        self._refused = False

    def reserve(self, role, *args, **kw):
        if role == "reflect" and self._refused:
            held = super().reserve(role, *args, **kw)   # raises once the fix is in; counted only if the call is made
            self.reflect_after_refusal += 1
            return held
        try:
            return super().reserve(role, *args, **kw)
        except BudgetExceeded:
            self._refused = self._refused or role == "task"
            raise


def test_no_reflection_after_a_refusal():
    b = Budget(max_task_calls=1, max_reflection_calls=5)
    b.reserve("task")
    with pytest.raises(BudgetExceeded):
        b.reserve("task")
    with pytest.raises(BudgetExceeded):
        b.reserve("reflect")
    f = Budget()
    f.reserve("task")
    f.fail("task", OSError("HTTP Error 429"))
    with pytest.raises(BudgetExceeded):
        f.reserve("reflect")
    assert f.calls == {"task": 1}


def test_dspy_search_ends_when_the_cap_refuses_a_call(tmp_path, monkeypatch):
    made = []

    def small(**kw):
        made.append(_Watched(**{**kw, "max_task_calls": 40}))
        return made[-1]

    monkeypatch.setattr(run, "Budget", small)
    out = run.main(["--runs", "1", "--out", str(tmp_path)])
    assert "The search ended at: spend cap, call refused" in (out / "report.md").read_text()
    # No paid reflection after the refusal: it would rewrite a prompt that "failed" only because its calls were
    # refused. Before the fix, 3 reflection calls were made in this run, some of them after the refusal.
    assert made[0].reflect_after_refusal == 0
    assert made[0].calls["task"] == 40
    assert made[0].calls["test-before"] == made[0].calls["test-after"] == 6


# One task call fails part-way through the search, as a single 429 from the gateway would.
FLAKY = """
import sys, threading
sys.path.insert(0, "tests")
from conftest import load_script
from main import stub
real, n, lock = stub.task_reply, [0], threading.Lock()
def flaky(*a):
    with lock:
        n[0] += 1
        k = n[0]
    if k == 30:
        raise OSError("HTTP Error 429: Too Many Requests")
    return real(*a)
stub.task_reply = flaky
if sys.argv[1] == "direct":
    m = load_script("ext-gepa-direct/optimize_direct.py")
else:
    from main import run as m
m.main(["--runs", "1", "--out", sys.argv[2]])
"""


@pytest.mark.parametrize("runner", ["direct", "main"])
def test_a_failed_call_cuts_the_search_and_keeps_its_result(tmp_path, runner):
    done = subprocess.run([sys.executable, "-c", FLAKY, runner, str(tmp_path)], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report = (tmp_path / "report.md").read_text()
    end = [line for line in report.splitlines() if line.startswith("The search ended at:")][0]
    assert "failed, so the search was cut there" in end and "429" in end
    assert re.search(r"model call \d+ ", end)
    assert (tmp_path / "instructions_after.txt").read_text().strip()
    assert json.loads((tmp_path / "candidates.json").read_text())
    assert "| before |" in report and "| after |" in report


def test_report_says_what_ended_the_search(tmp_path):
    out = run.main(["--runs", "1", "--out", str(tmp_path / "a"), "--max-reflection-calls", "1"])
    assert "The search ended at: reflection cap (1 calls)" in (out / "report.md").read_text()
    out = run.main(["--runs", "1", "--out", str(tmp_path / "b"), "--max-metric-calls", "20",
                    "--max-reflection-calls", "50"])
    assert "The search ended at: the budget of metric calls" in (out / "report.md").read_text()
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    out = direct.main(["--runs", "1", "--out", str(tmp_path / "c")])
    assert "The search ended at: reflection cap (8 calls)" in (out / "report.md").read_text()


def test_a_model_without_a_price_is_refused_before_any_call(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        run.main(["--runs", "1", "--out", str(tmp_path), "--task-model", "some-other-model"])
    assert "no price for model 'some-other-model'" in str(stopped.value)
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    with pytest.raises(SystemExit):
        direct.main(["--runs", "1", "--out", str(tmp_path), "--task-model", "some-other-model"])
    assert not list(tmp_path.iterdir())


@pytest.fixture
def refusing_gateway(monkeypatch):
    """A local stand-in that refuses every call the way the gateway refuses an unknown token."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.path)
            out = json.dumps({"error": {"message": "unknown key", "type": "invalid_request_error",
                                        "code": "invalid_api_key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GATEWAY_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "test-token-not-real")
    yield seen
    server.shutdown()


QUESTIONS = [{"id": "t1", "question": "How many orders were placed?", "sql": "SELECT COUNT(*) FROM orders"}]


def test_refused_gateway_call_is_an_error_not_a_failed_answer(refusing_gateway):
    budget = Budget()
    with pytest.raises(ModelCallFailed) as failed:
        gateway_chat("qwen3.6-35b-a3b", "", "hello", "task", budget)
    assert "401" in str(failed.value)
    assert budget.stop() is True and "401" in budget.failed
    # The test report does not turn it into a row that says "SQL error": the run stops.
    with pytest.raises(ModelCallFailed):
        evaluate(lambda q: gateway_chat("qwen3.6-35b-a3b", "", q, "test-before", budget), QUESTIONS, runs=1)


def test_refused_gateway_call_ends_the_direct_run(refusing_gateway, tmp_path):
    # The seed's validation run is refused: there is no search result, so the run ends with one line.
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    with pytest.raises(SystemExit) as stopped:
        direct.main(["--target", "gateway", "--runs", "1", "--out", str(tmp_path)])
    assert "401" in str(stopped.value) and "the search stopped before it had a result" in str(stopped.value)
    assert not (tmp_path / "report.md").exists()
    assert len(refusing_gateway) <= 2


def test_test_runs_that_fail_still_leave_the_search_result_and_a_report(tmp_path, monkeypatch):
    from main import stub

    real = stub.task_reply

    def refuse_tests(model, system, user):
        if refuse_tests.testing:
            raise OSError("HTTP Error 401: Unauthorized")
        return real(model, system, user)

    refuse_tests.testing = False
    monkeypatch.setattr(stub, "task_reply", refuse_tests)
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    load_test = direct.load_test

    def start_tests():
        refuse_tests.testing = True
        return load_test()

    monkeypatch.setattr(direct, "load_test", start_tests)
    with pytest.raises(SystemExit) as stopped:
        direct.main(["--runs", "1", "--out", str(tmp_path)])
    assert str(stopped.value).startswith("the test runs stopped: test-before: OSError")
    report = (tmp_path / "report.md").read_text()
    assert "The test runs stopped before they were done" in report and "before, after" in report
    assert (tmp_path / "instructions_after.txt").exists() and (tmp_path / "candidates.json").exists()


def test_refused_dspy_call_is_recorded_and_not_scored(refusing_gateway, tmp_path):
    from main.program import SQLAnswerer
    from main.report import program_answer

    budget = Budget()
    lm = make_lm("qwen3.6-35b-a3b", "test-before", budget, offline=False)
    lm.num_retries = 0
    with pytest.raises(Exception) as failed:
        evaluate(program_answer(SQLAnswerer(), lm), QUESTIONS, runs=1)
    assert budget.failed and budget.stop() is True
    assert "test-before" in str(failed.value) or "test-before" in budget.failed
