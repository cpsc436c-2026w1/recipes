"""Steps for clean_up.py and the routing eval (ext-eval/routing_eval.py)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil

import routing_eval
from behave import given, then, when
from support import patch, run_captured

import clean_up
import main.model as model_module
from main.config import ROOT, Settings

LOGS = ("misroutes.jsonl", "routes.jsonl", "eval-misroutes.jsonl")


def _hashes(folder) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


@given("a copy of the recipe's output folder with the logs a run writes")
def output_copy(context):
    context.copy = context.tmp / "recipe"
    shutil.copytree(ROOT / "output", context.copy / "output", ignore=shutil.ignore_patterns(*LOGS))
    context.kept_runs = _hashes(context.copy / "output")
    assert context.kept_runs, "the recipe's output folder has no captured run"
    for name in LOGS:
        (context.copy / "output" / name).write_text('{"reason": "a line a run wrote"}\n')


@when("clean_up.py runs")
def clean_up_runs(context):
    run_captured(context, clean_up.main, [], root=context.copy)


@when('clean_up.py runs with "{args}"')
def clean_up_runs_with(context, args):
    run_captured(context, clean_up.main, shlex.split(args), root=context.copy)


@then('the copy has no ".jsonl" file')
def no_logs(context):
    assert not list((context.copy / "output").glob("*.jsonl"))


@then("the copy still has the logs")
def still_logs(context):
    assert sorted(p.name for p in (context.copy / "output").glob("*.jsonl")) == sorted(LOGS)


@then("the captured runs in the copy are as they were")
def captured_as_before(context):
    assert _hashes(context.copy / "output") == context.kept_runs


# ---- the eval ----

def _run_eval(context, extra: list[str]) -> None:
    context.report_path, context.eval_log = context.tmp / "eval.md", context.tmp / "eval-misroutes.jsonl"
    context.model_names = []
    built_chat = model_module.StubModel.chat

    def chat(self, model, system, user, max_tokens):          # counts calls; the stub answers as it does
        context.model_names.append(model)
        return built_chat(self, model, system, user, max_tokens)

    patch(context, model_module.StubModel, "chat", chat)
    run_captured(context, routing_eval.main,
                 ["--out", str(context.report_path), "--log", str(context.eval_log), *extra])


@when("the routing eval runs on the stub")
def eval_runs(context):
    _run_eval(context, [])


@when('the routing eval runs with "{args}"')
def eval_runs_gateway(context, args):
    _run_eval(context, shlex.split(args))


@then("no report was written")
def no_report(context):
    assert not context.report_path.exists()


@when('the routing eval runs on the stub with "{args}"')
def eval_runs_with(context, args):
    _run_eval(context, shlex.split(args))


@then('the report holds "{text}"')
def report_holds(context, text):
    report = context.report_path.read_text()
    assert text in report and text in context.printed, report


@then('the report\'s row "{policy}" has {calls:d} large-model calls and router cost {cost}')
def report_row(context, policy, calls, cost):
    row = next(line for line in context.report_path.read_text().splitlines() if line.startswith(f"| {policy} |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert (cells[3], cells[4]) == (str(calls), cost), row
    assert re.fullmatch(r"0\.\d{8}", cells[1]) and re.fullmatch(r"0\.\d{6}", cells[2]), row


@then("the large model was called {n:d} times in the eval")
def large_called(context, n):
    assert context.model_names.count(Settings().large_model) == n, context.model_names.count(Settings().large_model)


@then('the eval\'s log has {n:d} lines with reason "{reason}"')
def eval_log_reason(context, n, reason):
    lines = [json.loads(x) for x in context.eval_log.read_text().splitlines()]
    assert sum(x["reason"] == reason for x in lines) == n, lines


@then('the eval\'s log names the requests "{ids}" for that reason')
def eval_log_ids(context, ids):
    lines = [json.loads(x) for x in context.eval_log.read_text().splitlines()]
    got = sorted(x["request_id"] for x in lines if x["reason"] == "label differs from the hand label")
    assert got == [i.strip() for i in ids.split(",")], got


@then("the report's paths have {n:d} requests in all, and their shares add up to {total:g}")
def report_paths(context, n, total):
    report = context.report_path.read_text()
    table = report[report.index("| path | requests |"):].splitlines()[2:6]
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in table]
    assert [c[0] for c in cells] == ["lookup", "search", "hard", "out_of_scope"], cells
    assert sum(int(c[1]) for c in cells) == n and abs(sum(float(c[3]) for c in cells) - total) < 0.011, cells
