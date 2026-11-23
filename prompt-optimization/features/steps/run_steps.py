"""Steps for main/run.py, the two extension scripts and clean_up.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import time
import shlex
import shutil
import socket

from behave import given, then, when
from support import TOKEN, FakeGateway, patch, run_captured

import clean_up
import main.run as run_module
from main import stub
from main.data import SCHEMA, load_test
from main.models import PRICES_CAD
from main.program import SEED_INSTRUCTIONS

ROOT = run_module.ROOT


def _load_script(rel: str):
    """Import an extension script by path (its folder name has a hyphen)."""
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


direct = _load_script("ext-gepa-direct/optimize_direct.py")
matrix = _load_script("ext-eval-matrix/matrix.py")
TEST_QUESTIONS = [q["question"] for q in load_test()]


# ---- given ----

@given("the main scenario has run on the stub")
def main_has_run(context):
    """One run with the defaults, made once and read by every scenario that changes nothing."""
    if "main" not in context.runs:
        saved = (context.printed, context.printed_err)
        context.printed = context.printed_err = ""
        out = context.shared / "main"
        run_captured(context, run_module.main, ["--out", str(out)])
        context.runs["main"] = (out, context.printed, context.printed_err, context.exit_code)
        context.printed, context.printed_err = saved
    context.out, context.printed, context.printed_err, context.exit_code = context.runs["main"]


@given("every call to the stub models is recorded")
def record_calls(context):
    real_task, real_reflect = stub.task_reply, stub.reflect_reply

    def task(model, system, user):
        context.calls.append(("task", model, system, user))
        return real_task(model, system, user)

    def reflect(prompt):
        context.calls.append(("reflect", None, "", prompt))
        return real_reflect(prompt)

    patch(context, stub, "task_reply", task)
    patch(context, stub, "reflect_reply", reflect)
    for module in (run_module, direct, matrix):
        real_load = module.load_test

        def load(real_load=real_load):
            context.calls.append(("load_test", None, "", ""))
            return real_load()

        patch(context, module, "load_test", load)


@given('the stub task model returns nothing for "{question}"')
def stub_returns_nothing(context, question):
    real = stub.task_reply
    patch(context, stub, "task_reply", lambda model, system, user: "" if question in user else real(model, system, user))


@given('the stub task model fails its {n:d}th call with "{message}"')
def stub_fails_nth(context, n, message):
    real, count = stub.task_reply, {"n": 0}

    def reply(model, system, user):
        count["n"] += 1
        if count["n"] == n:
            raise OSError(message)
        return real(model, system, user)

    patch(context, stub, "task_reply", reply)


@given("every call to the stub task model takes {seconds:g} seconds")
def stub_is_slow(context, seconds):
    """As a model is: the four threads of an evaluation then have their calls in flight at the same time."""
    real = stub.task_reply

    def reply(model, system, user):
        time.sleep(seconds)
        return real(model, system, user)

    patch(context, stub, "task_reply", reply)


@given("the stub task model fails every call once the test questions are loaded")
def stub_fails_in_tests(context):
    real, real_load, state = stub.task_reply, run_module.load_test, {"testing": False}

    def reply(model, system, user):
        if state["testing"]:
            raise OSError("HTTP Error 401: Unauthorized")
        return real(model, system, user)

    def load():
        state["testing"] = True
        return real_load()

    patch(context, stub, "task_reply", reply)
    patch(context, run_module, "load_test", load)


def _gateway(context, **kw):
    context.gateway = FakeGateway(**kw)
    os.environ["COURSE_GATEWAY_URL"], os.environ["KB_MCP_TOKEN"] = context.gateway.url, TOKEN


@given("its address and the token are set as GATEWAY_URL and COURSE_MCP_TOKEN only")
def older_names(context):
    os.environ["GATEWAY_URL"], os.environ["COURSE_MCP_TOKEN"] = context.gateway.url, TOKEN
    for name in ("COURSE_GATEWAY_URL", "KB_MCP_TOKEN"):
        os.environ.pop(name, None)


@given("a gateway stand-in that answers as the stub does")
def gateway_answers(context):
    _gateway(context)


@given("a gateway stand-in whose reflection replies are empty, cut at the token cap")
def gateway_empty_reflection(context):
    _gateway(context, empty_reflection=True)


@given("a gateway stand-in that refuses every request after the first {n:d} with HTTP {status:d}")
def gateway_refuses(context, n, status):
    _gateway(context, refuse_after=n, status=status)


@given("{variable} is not set")
def not_set(context, variable):
    os.environ.pop(variable, None)


@given("no course token and no gateway address in the environment")
def no_token(context):
    assert not {"COURSE_MCP_TOKEN", "GATEWAY_URL", "KB_MCP_TOKEN", "COURSE_GATEWAY_URL"} & set(os.environ)


@given("every network connection fails the scenario")
def no_network(context):
    def refuse(self, address):
        raise AssertionError(f"the run opened a network connection to {address}")

    patch(context, socket.socket, "connect", refuse)


@given("a tuned prompt saved from the main scenario's stub run")
def tuned_prompt(context):
    main_has_run(context)
    context.tuned = context.out / "instructions_after.txt"
    context.printed = context.printed_err = ""


# ---- when ----

def _run(context, fn, argv):
    context.out = context.tmp / "run"
    run_captured(context, fn, [*argv, "--out", str(context.out)])


@when('main/run.py runs with "{args}"')
def main_runs(context, args):
    _run(context, run_module.main, shlex.split(args))


@when("ext-gepa-direct/optimize_direct.py runs")
def direct_runs(context):
    _run(context, direct.main, [])


@when('ext-gepa-direct/optimize_direct.py runs with "{args}"')
def direct_runs_with(context, args):
    _run(context, direct.main, shlex.split(args))


@when('ext-eval-matrix/matrix.py runs with the tuned prompt as "{name}"')
def matrix_runs_tuned(context, name):
    _run(context, matrix.main, ["--prompt", f"{name}={context.tuned}"])


@when('ext-eval-matrix/matrix.py compares the seed with the tuned prompt, with "{args}"')
def matrix_runs_tuned_with(context, args):
    _run(context, matrix.main, ["--prompt", f"after={context.tuned}", *shlex.split(args)])


@when('ext-eval-matrix/matrix.py runs with "{args}"')
def matrix_runs_with(context, args):
    _run(context, matrix.main, shlex.split(args))


# ---- then: the run folder ----

def _report(context) -> str:
    return (context.out / "report.md").read_text()


def _spend(context) -> dict:
    return json.loads((context.out / "spend.json").read_text())


@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed_err[-500:], context.error)


@then('the run folder holds "{names}"')
def folder_holds(context, names):
    assert sorted(p.name for p in context.out.iterdir()) == [n.strip() for n in names.split(",")], \
        sorted(p.name for p in context.out.iterdir())


@then("no run folder was written")
def no_folder(context):
    assert not context.out.exists()


@then('the printed output holds the report and ends with "{text}" and the run folder')
def printed_report(context, text):
    assert _report(context) in context.printed
    assert context.printed.rstrip().splitlines()[-1] == f"{text} {context.out}"


@then('the error output holds "{text}"')
def err_holds(context, text):
    assert text in context.printed_err, context.printed_err[-600:]


@then('the printed output holds "{text}"')
def out_holds(context, text):
    assert text in context.printed, context.printed[-400:]


@then('the run ended with the error "{kind}" that holds "{text}"')
def ended_with_error(context, kind, text):
    assert context.error is not None and type(context.error).__name__ == kind and text in str(context.error), \
        repr(context.error)


@then('the candidates\' validation scores are "{scores}"')
def candidate_scores(context, scores):
    candidates = json.loads((context.out / "candidates.json").read_text())
    assert [f"{100 * c['val_score']:.0f}%" for c in candidates] == [s.strip() for s in scores.split(",")]


@then("instructions_after.txt holds the instructions of the candidate that scored {score}")
def after_is_best(context, score):
    candidates = json.loads((context.out / "candidates.json").read_text())
    best = [c for c in candidates if f"{100 * c['val_score']:.0f}%" == score]
    after = (context.out / "instructions_after.txt").read_text().strip()
    assert len(best) == 1 and best[0]["instructions"].strip() == after and after != SEED_INSTRUCTIONS.strip()


@then("instructions_before.txt holds the seed instructions")
def before_is_seed(context):
    assert (context.out / "instructions_before.txt").read_text().strip() == SEED_INSTRUCTIONS.strip()


@then("instructions_after.txt is not empty")
def after_not_empty(context):
    assert (context.out / "instructions_after.txt").read_text().strip()


# ---- then: the report ----

@then('the report\'s heading says "{label}"')
def report_heading(context, label):
    assert f"## Test pass rate ({label})" in _report(context), _report(context)[:300]


def _row(context, name) -> list[str] | None:
    for line in _report(context).splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if line.startswith(f"| {name} |") and len(cells) == 6:
            return cells
    return None


@then('the report\'s row "{name}" is "{cells}"')
def report_row(context, name, cells):
    row = _row(context, name)
    assert row is not None and row[1:5] == [c.strip() for c in cells.split("|")], row
    assert re.fullmatch(r"0\.\d{6}", row[5]) and float(row[5]) > 0, row


@then('the report\'s row "{name}" names the failure "{failure}"')
def report_row_failure(context, name, failure):
    row = _row(context, name)
    assert row is not None and failure in [f.strip() for f in row[4].split(",")], row


@then('the report has no row "{name}"')
def report_no_row(context, name):
    assert _row(context, name) is None


@then('the report holds "{text}"')
def report_holds(context, text):
    assert text in _report(context), _report(context)[-700:]


@then('the report lists the candidates under "{heading}"')
def report_candidates(context, heading):
    report = _report(context)
    assert heading in report
    assert re.findall(r"^- #\d+: \d+%$", report[report.index(heading):], flags=re.M) == ["- #0: 33%", "- #1: 67%",
                                                                                         "- #2: 100%"]


@then("the test table holds no validation score")
def table_without_validation(context):
    report = _report(context)
    table = report[:report.index("## Spend")]
    assert "validation" not in table.lower() and "100%" not in table


@then('the report\'s last line is "{text}"')
def report_last_line(context, text):
    assert _report(context).rstrip().splitlines()[-1] == text, _report(context).rstrip().splitlines()[-1]


@then('the report\'s last line starts with "{text}"')
def report_last_line_starts(context, text):
    assert _report(context).rstrip().splitlines()[-1].startswith(text), _report(context).rstrip().splitlines()[-1]


@then('the report\'s last line ends with "{text}"')
def report_last_line_ends(context, text):
    assert _report(context).rstrip().splitlines()[-1].endswith(text), _report(context).rstrip().splitlines()[-1]


@then('the report\'s last line holds "{text}"')
def report_last_line_holds(context, text):
    assert text in _report(context).rstrip().splitlines()[-1]


# ---- then: the spend ----

@then('the spend file counts {n:d} calls for "{role}"')
def spend_calls(context, n, role):
    assert _spend(context)["calls"].get(role, 0) == n, _spend(context)["calls"]


@then('the spend file counts between {low:d} and {high:d} calls for "{role}"')
def spend_calls_between(context, low, high, role):
    assert low <= _spend(context)["calls"].get(role, 0) <= high, _spend(context)["calls"]


@then('the report\'s spend table has the roles "{roles}" and a total of {n:d} calls')
def spend_table(context, roles, n):
    report = _report(context)
    section = report[report.index("## Spend") + 3:]
    lines = section[:section.index("\n## ")].splitlines()            # up to the next heading
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in lines if line.startswith("| ")][1:]
    assert [r[0] for r in rows] == [*(x.strip() for x in roles.split(",")), "total"], rows
    assert int(rows[-1][1]) == n == sum(int(r[1]) for r in rows[:-1]), rows
    assert all(re.fullmatch(r"[\d,]+", r[2]) and re.fullmatch(r"[\d,]+", r[3]) for r in rows[:-1]), rows


@then("the report's total in CAD is the sum of the roles in the spend file")
def spend_total(context):
    total = float(re.search(r"^\| total \| \d+ \| \| \| ([\d.]+) \|$", _report(context), flags=re.M).group(1))
    assert abs(total - sum(_spend(context)["cad"].values())) < 1e-6 and total > 0


@then("the search spent between {low:g} and {high:g} CAD")
def search_spent(context, low, high):
    cad = _spend(context)["cad"]
    assert low <= cad.get("task", 0) + cad.get("reflect", 0) <= high, cad


@then("the whole run spent less than {cap:g} CAD")
def run_spent(context, cap):
    assert 0 < sum(_spend(context)["cad"].values()) < cap, _spend(context)["cad"]


@then("the whole run spent more than nothing and at most {cap:g} CAD")
def run_spent_at_most(context, cap):
    assert 0 < sum(_spend(context)["cad"].values()) <= cap, _spend(context)["cad"]


@then("the spend file holds nothing for calls in flight")
def spend_nothing_held(context):
    # Every call gives back what reserve() held for it, by settle() or by fail(). A hold that is never given back
    # would refuse later calls for money that was never spent.
    assert _spend(context)["held"] == 0, _spend(context)


@then("the spend file counts no call and no money")
def run_spent_nothing(context):
    spend = _spend(context)
    assert sum(spend["calls"].values()) == 0 and sum(spend["cad"].values()) == 0, spend


@then('the spend file prices "{role}" as "{model}"')
def spend_priced_as(context, role, model):
    spend = _spend(context)
    t_in, t_out = spend["tokens"][role]
    p_in, p_out = PRICES_CAD[model]
    assert abs(spend["cad"][role] - (t_in * p_in + t_out * p_out) / 1e6) < 1e-9 and spend["cad"][role] > 0


# ---- then: what the stub models were asked ----

def _before_and_after_load(context):
    marks = [i for i, c in enumerate(context.calls) if c[0] == "load_test"]
    assert len(marks) == 1, f"the test questions were loaded {len(marks)} times"
    return context.calls[:marks[0]], context.calls[marks[0] + 1:]


@then("the test questions were loaded once, after the last reflection call")
def loaded_once(context):
    before, after = _before_and_after_load(context)
    assert any(c[0] == "reflect" for c in before) and not any(c[0] == "reflect" for c in after)


@then("no call before that carried a test question")
def no_test_question_before(context):
    before, _ = _before_and_after_load(context)
    assert before and not [q for q in TEST_QUESTIONS for c in before if q in c[2] + c[3]]


@then("every test question was asked after it")
def test_questions_after(context):
    _, after = _before_and_after_load(context)
    assert all(any(q in c[3] for c in after) for q in TEST_QUESTIONS)


@then("every task call carried the table description as written")
def schema_in_every_call(context):
    tasks = [c for c in context.calls if c[0] == "task"]
    assert len(tasks) > 50 and all(SCHEMA in c[3] for c in tasks)


@then("the task calls carried {n:d} different system prompts, one per candidate, each with the same field markers")
def system_prompts(context, n):
    systems = {c[2] for c in context.calls if c[0] == "task"}
    candidates = json.loads((context.out / "candidates.json").read_text())
    assert len(systems) == n == len(candidates), (len(systems), len(candidates))
    for system in systems:
        assert "[[ ## question ## ]]" in system and "[[ ## tables ## ]]" in system and "[[ ## sql ## ]]" in system
        # the seed's first sentence is in every candidate: the stub's reflection only adds rules after it
        assert SEED_INSTRUCTIONS.split(".")[0] in " ".join(system.split())


@then('the report has no row "{name}" with the failure "{kind}" counted for the failed call')
def failed_call_not_scored(context, name, kind):
    """The stub's 6 test questions fail as SQL error 2, wrong unit 2, wrong value 1 with the seed prompt. A failed
    call that was scored would add one to a failure type."""
    row = _row(context, name)
    assert row is not None and row[4] == "SQL error 2, wrong unit 2, wrong value 1", row


@then("every task call's system prompt started with the fixed head of the prompt")
def head_unchanged(context):
    tasks = [c for c in context.calls if c[0] == "task"]
    assert len(tasks) > 50 and all(c[2].startswith(direct.HEAD) for c in tasks)


@then("no call was made to a stub model")
def no_stub_call(context):
    assert context.calls == []


@then("{n:d} calls were made to a stub model, none of them a reflection call")
def stub_calls(context, n):
    kinds = [c[0] for c in context.calls if c[0] != "load_test"]
    assert len(kinds) == n and set(kinds) == {"task"}, kinds


# ---- then: the gateway stand-in ----

@then("the gateway received {n:d} requests")
def gateway_received(context, n):
    assert len(context.gateway.requests) == n, len(context.gateway.requests)


@then("the gateway received fewer than {n:d} requests")
def gateway_received_fewer(context, n):
    assert 0 < len(context.gateway.requests) < n, len(context.gateway.requests)


@then('every gateway request went to "{path}" with the course token as the bearer key')
def gateway_path(context, path):
    assert {(r["path"], r["auth"]) for r in context.gateway.requests} == {(path, f"Bearer {TOKEN}")}


@then('the gateway requests named the models "{models}"')
def gateway_models(context, models):
    assert sorted({r["body"]["model"] for r in context.gateway.requests}) == [m.strip() for m in models.split(",")]


@then('{n:d} gateway requests named the model "{model}"')
def gateway_model_count(context, n, model):
    assert sum(r["body"]["model"] == model for r in context.gateway.requests) == n


@then("every gateway request caps the output at {n:d} tokens")
def gateway_cap(context, n):
    caps = {r["body"].get("max_tokens") or r["body"].get("max_completion_tokens") for r in context.gateway.requests}
    assert caps == {n}, caps


@then("every task-model request caps the output at {task:d} tokens and every reflection request at {reflect:d}")
def gateway_caps_by_role(context, task, reflect):
    caps = {}
    for r in context.gateway.requests:
        cap = r["body"].get("max_tokens") or r["body"].get("max_completion_tokens")
        caps.setdefault(r["body"]["model"], set()).add(cap)
    assert caps == {"qwen3.6-35b-a3b": {task}, "qwen3.8-27b": {reflect}}, caps


@then("the course token is in no printed line and in no file of the run folder")
def token_nowhere(context):
    files = "".join(p.read_text() for p in context.out.iterdir())
    assert files and TOKEN not in context.printed + context.printed_err + files


# ---- the eval matrix ----

def _matrix_rows(context) -> dict[str, list[str]]:
    rows, inside = {}, False
    for line in _report(context).splitlines():
        if line.startswith("## "):   # only the matrix table: the comparison tables also have 8 columns
            inside = line.startswith("## Eval matrix (")
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if inside and line.startswith("| ") and len(cells) == 8 and cells[0] not in ("model", "---"):
            rows[f"{cells[0]} | {cells[1]}"] = cells[2:]
    return rows


@then("the matrix has {n:d} rows")
def matrix_rows(context, n):
    assert len(_matrix_rows(context)) == n, _matrix_rows(context)


@then('the matrix row "{cell}" is "{cells}"')
def matrix_row(context, cell, cells):
    assert _matrix_rows(context)[cell] == [c.strip() for c in cells.split("|")], _matrix_rows(context).get(cell)


# ---- clean_up.py ----

def _hashes(folder) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _copy(context, with_runs: bool):
    context.copy = context.tmp / "recipe"
    shutil.copytree(ROOT / "output", context.copy / "output")
    context.kept_runs = _hashes(context.copy / "output")
    assert context.kept_runs
    if with_runs:
        for name in ("20261113-101500", "direct-20261113-101700"):
            (context.copy / "runs" / name).mkdir(parents=True)
            (context.copy / "runs" / name / "report.md").write_text("a report a run wrote\n")


@given("a copy of the recipe folder with two run folders under runs/")
def copy_with_runs(context):
    _copy(context, True)


@given("a copy of the recipe folder without a runs folder")
def copy_without_runs(context):
    _copy(context, False)


@when("clean_up.py runs")
def clean_up_runs(context):
    run_captured(context, clean_up.main, ["--yes"], root=context.copy)      # a bare run only lists (the contract)


@then("the copy has no runs folder")
def no_runs(context):
    assert not (context.copy / "runs").exists()


@then("the captured runs in the copy's output folder are as they were")
def captured_same(context):
    assert _hashes(context.copy / "output") == context.kept_runs


# ---- the per-question table ----

@given('the stub task model returns nothing for "{question}" once the instructions have a rule added')
def stub_breaks_question(context, question):
    real = stub.task_reply

    def reply(model, system, user):
        tuned = any(words in " ".join(system.lower().split()) for words in stub.STOPPED_BY.values())
        return "" if tuned and question in user else real(model, system, user)

    patch(context, stub, "task_reply", reply)


def _question_rows(context) -> dict[str, list[str]]:
    report = _report(context)
    section = report[report.index("## Per question"):report.index("## Candidates")]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if line.startswith("| q") and len(cells) == 4:
            rows[cells[0]] = cells[2:]
    return rows


@then('the report\'s per-question table is headed "{text}"')
def per_question_heading(context, text):
    assert f"## Per question ({text})" in _report(context)
    assert list(_question_rows(context)) == [q["id"] for q in load_test()]


@then('the per-question row "{qid}" is "{cells}"')
def per_question_row(context, qid, cells):
    assert _question_rows(context)[qid] == [c.strip() for c in cells.split("|")], _question_rows(context)


# ---- run.json ----

def _manifest(context) -> dict:
    return json.loads((context.out / "run.json").read_text())


@then('run.json names the task model "{task}", the reflection model "{reflect}" and the target "{target}"')
def manifest_models(context, task, reflect, target):
    m = _manifest(context)
    assert (m["task_model"], m["reflection_model"], m["target"]) == (task, reflect, target), m
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", m["written_at"])


@then("run.json names the versions of dspy and gepa that are installed")
def manifest_packages(context):
    from importlib.metadata import version

    assert _manifest(context)["packages"] == {"dspy": version("dspy"), "gepa": version("gepa")}


@then("run.json holds the hash of instructions_after.txt, of the seed and of the two question files")
def manifest_hashes(context):
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    assert _manifest(context)["sha256_16"] == {
        "data/questions.yml": sha(ROOT / "data" / "questions.yml"), "data/test.yml": sha(ROOT / "data" / "test.yml"),
        "instructions_before": sha(context.out / "instructions_before.txt"),
        "instructions_after": sha(context.out / "instructions_after.txt")}


@then('run.json holds the caps "{caps}"')
def manifest_caps(context, caps):
    expected = {k: float(v) if "." in v else int(v) for k, v in (c.strip().split(" ") for c in caps.split(","))}
    assert _manifest(context)["caps"] == expected, _manifest(context)["caps"]


@then('run.json says the search ended at "{ended}" and gives the test medians of both prompts')
def manifest_end(context, ended):
    m = _manifest(context)
    assert m["search_ended"] == ended, m["search_ended"]
    assert set(m["test_median"]) == {"before", "after"} and m["test_median"]["after"] > m["test_median"]["before"]


@then("run.json holds no question text")
def manifest_no_text(context):
    text = (context.out / "run.json").read_text()
    assert not [q for q in TEST_QUESTIONS if q in text] and "SELECT" not in text


@then("the matrix's spend file counts {n:d} calls in all, and its money is the total in the report")
def matrix_spend(context, n):
    spend = json.loads((context.out / "spend.json").read_text())
    found = re.search(r"cut included: ([\d.]+) CAD in (\d+) calls", _report(context))
    total = float(found.group(1))
    assert sum(spend["calls"].values()) == n == int(found.group(2)), (spend["calls"], found.group(2))
    assert abs(total - sum(spend["cad"].values())) < 1e-6 and total > 0, (total, spend["cad"])


@then("the gateway received more than {n:d} requests, and none of them twice after the first {m:d}")
def gateway_no_resend(context, n, m):
    refused = [json.dumps(r["body"], sort_keys=True) for r in context.gateway.requests[m:]]
    assert len(context.gateway.requests) > n and len(refused) == len(set(refused)), \
        f"{len(refused)} refused requests, {len(set(refused))} different"
