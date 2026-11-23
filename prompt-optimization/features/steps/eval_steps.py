"""Steps for the eval matrix with groups (ext-eval-matrix: groups.py, score.py, templates.py, the rides case)."""

from __future__ import annotations

import json
import os
import shlex

from behave import given, then, when
from support import patch, run_captured

from run_steps import ROOT, _report, _run, matrix

import groups      # ext-eval-matrix/, on the path once matrix.py is loaded
import rides
import score
import templates

RIDES = ["--case", "rides"]


def _table(rows) -> list[dict]:
    return [{h: (r[h] or "").strip() for h in rows.headings} for r in rows]


def _md_rows(text: str, columns: int) -> list[list[str]]:
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == columns and cells[0] not in ("group", "cell") \
                and not cells[0].startswith("---"):
            out.append(cells)
    return out


def _section(report: str, heading: str) -> str:
    """The text under the first heading that starts with `heading`, up to the next heading of the same level."""
    level = heading.split(" ")[0]
    lines, inside = [], False
    for line in report.splitlines():
        if line.startswith(heading):
            inside = True
            continue
        if inside and line.startswith(level + " "):
            break
        if inside:
            lines.append(line)
    assert inside, f"no heading {heading!r}"
    return "\n".join(lines)


# ---- given ----

@given("a rides test file with these cases:")
def rides_test_file(context):
    path = context.tmp / "test.yml"
    lines = []
    for r in _table(context.table):
        lines += [f"- id: {r['id']}", f"  group: {r['group']}" if r["group"] else "  group:",
                  f"  question: {r['question']}"]
    path.write_text("\n".join(lines) + "\n")
    context.case_file = path


def _runs(case: str, group: str, answerable: bool, results: list[str]) -> list[dict]:
    rows = []
    for k, r in enumerate(results):
        refused = r == "refused"
        if answerable:
            outcome = "correct" if r == "right" else "wrong"
        else:
            outcome = "correct refusal" if refused else "wrong"
        rows.append({"case": case, "group": group, "answerable": answerable, "run": k + 1, "outcome": outcome,
                     "refused": refused, "seconds": 0.0, "cad": 0.0})
    return rows


@given('cell "{cell}" has these runs:')
def cell_runs(context, cell):
    rows = []
    for r in _table(context.table):
        rows += [dict(x, cell=cell) for x in _runs(r["case"], r["group"], r["answerable"] == "yes",
                                                   [r["run 1"], r["run 2"], r["run 3"]])]
    context.cell, context.cell_rows = cell, rows


@given('cells "{first}" and "{second}" have these runs:')
def two_cells(context, first, second):
    rows = []
    for r in _table(context.table):
        for cell, column in ((first, "runs passed of 3, first"), (second, "runs passed of 3, second")):
            n = int(r[column])
            rows += [dict(x, cell=cell) for x in _runs(r["case"], r["group"], True,
                                                       ["right"] * n + ["wrong"] * (3 - n))]
    path = context.tmp / "cases.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in sorted(rows, key=lambda x: x["cell"] != first)))
    context.saved = path


@given('the metric is "{metric}"')
def metric_is(context, metric):
    context.metric = metric


@given('a case has reference_ids "{refs}" and required_ids "{required}"')
def citation_case(context, refs, required):
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    context.case = {"id": "c", "reference_ids": split(refs), "required_ids": split(required)}


@given("every stub call of the rides SQL step costs {cad:g} CAD")
def stub_call_costs(context, cad):
    from main.models import PRICES_CAD
    patch(context, matrix, "stub_usage", lambda system, user, reply, model: (0, round(cad * 1e6 / PRICES_CAD[model][1])))


@given("every answer of the rides matrix takes {seconds:g} seconds")
def answers_take(context, seconds):
    calls = {"n": 0}

    def clock():  # start and end of one answer are `seconds` apart; the next answer starts where it ended
        calls["n"] += 1
        return seconds * (calls["n"] // 2)

    patch(context, matrix, "clock", clock)


@given('cell "{cell}" has 6 answers that took "{times}" s and cost {cad:g} CAD together')
def six_answers(context, cell, times, cad):
    seconds = [float(x) for x in times.split(",")]
    context.cell = cell
    context.cell_rows = [{"seconds": s, "cad": cad / len(seconds)} for s in seconds]


@given("the course LLM gateway from the environment")
def live_gateway(context):
    # @live only: the scenario reads the student's own token and the gateway's address (README, Run it live)
    url = os.environ.get("LIVE_COURSE_GATEWAY_URL") or os.environ.get("COURSE_GATEWAY_URL")
    token = os.environ.get("LIVE_KB_MCP_TOKEN") or os.environ.get("KB_MCP_TOKEN")
    assert url and token, "set COURSE_GATEWAY_URL and KB_MCP_TOKEN (LIVE_ names: behave clears the others)"
    os.environ["COURSE_GATEWAY_URL"], os.environ["KB_MCP_TOKEN"] = url, token


# ---- when ----

@when('the rides matrix runs with "{args}"')
def rides_matrix_runs(context, args):
    extra = ["--cases", str(context.case_file)] if getattr(context, "case_file", None) else []
    argv = [a.replace("ext-eval-matrix/", str(ROOT / "ext-eval-matrix") + "/") for a in shlex.split(args)]
    _run(context, matrix.main, [*RIDES, *extra, *argv])


@when("the cell's report is written")
def cell_report(context):
    context.cell_cases = groups.case_outcomes(context.cell_rows)
    context.cell_text = groups.group_table(context.cell, context.cell_cases)


@when("the cell's cost and time are written")
def cell_cost_time(context):
    context.cell_text = groups.cost_time_table({context.cell: context.cell_rows})


def _compare(context, args):
    context.printed = ""
    run_captured(context, groups.main, [str(context.saved), *shlex.split(args)])
    assert context.exit_code == 0, context.printed_err
    return context.printed


@when('the saved runs are compared with "{args}"')
def saved_compared(context, args):
    context.compared = _compare(context, args)


@when('the saved runs are compared twice with "{args}"')
def saved_compared_twice(context, args):
    context.first_table = _compare(context, args)
    context.compared = _compare(context, args)


@when('the run\'s cases.jsonl is compared with "{args}"')
def run_compared(context, args):
    context.saved = context.out / "cases.jsonl"
    context.compared = _compare(context, args)


@when("the metric scores each of these cited sets:")
def score_cited(context):
    context.scored = []
    for r in _table(context.table):
        cited = [x.strip() for x in r["cited_ids"].split(",") if x.strip()]
        context.scored.append((score.score(context.case, cited, context.metric).outcome, r["outcome"]))


@when("the value metric scores each of these answers:")
def score_values(context):
    context.scored = []
    for r in _table(context.table):
        case = {"id": "c", "answerable": r["reference"] != "none"}
        if case["answerable"]:
            case["reference"] = float(r["reference"])
        reply = "CANNOT_ANSWER" if r["the step returned"] == "CANNOT_ANSWER" else "SELECT 1"
        value = float(r["value"]) if r["value"] else None
        got = score.score(case, reply, "value", run_sql=lambda sql, v=value: (v, None))
        context.scored.append((got.outcome, r["outcome"]))


@when('the cell "no-model/templates" answers these questions:')
def templates_answer(context):
    trips = rides.Trips()
    context.answered = []
    for r in _table(context.table):
        reply = templates.answer(r["question"])
        if score.is_refusal(reply):
            got = ("CANNOT_ANSWER", "")
        else:
            value, error = trips.run_sql(reply)
            assert error is None, error
            got = ("a query", f"{value:g}")
        context.answered.append((got, (r["the step returned"], r["value"])))


# ---- then ----

@then("its group rows are:")
def group_rows_are(context):
    got = _md_rows(context.cell_text, 6)
    want = [[r[h] for h in context.table.headings] for r in _table(context.table)]
    assert got == want, got


@then("each cited set gets the outcome in its row")
@then("each answer gets the outcome in its row")
def scored_rows(context):
    assert all(got == want for got, want in context.scored), context.scored


@then("each answer has what the step returned and the value in its row")
def templates_rows(context):
    assert all(got == want for got, want in context.answered), context.answered


def _group_row(context, cell: str, group: str) -> list[str]:
    section = _section(_report(context), f"### {cell}")
    rows = {r[0]: r[1:] for r in _md_rows(section, 6)}
    return rows[group]


@then('the group row "{group}" of cell "{cell}" is "{cells}"')
def group_row_of_cell(context, group, cell, cells):
    assert _group_row(context, cell, group) == [c.strip() for c in cells.split("|")], _group_row(context, cell, group)


@then("cases.jsonl has {n:d} lines, {each:d} for each cell")
def cases_lines(context, n, each):
    rows = [json.loads(x) for x in (context.out / "cases.jsonl").read_text().splitlines()]
    counts = {}
    for r in rows:
        counts[r["cell"]] = counts.get(r["cell"], 0) + 1
        assert {"cell", "case", "group", "run", "outcome", "seconds", "cad"} <= set(r), r
    assert len(rows) == n and set(counts.values()) == {each}, counts


def _comparison_rows(context) -> dict[str, list[str]]:
    return {r[0]: r[1:] for r in _md_rows(context.compared, 8)}


@then("the comparison rows are:")
def comparison_rows(context):
    got = _comparison_rows(context)
    for r in _table(context.table):
        want = [r[h] for h in context.table.headings]
        assert got[want[0]] == want[1:], (want, got.get(want[0]))
    assert len(got) == len(context.table.rows), got


@then('the all-cases line gives the difference in runs passed "{diff}" with the descriptive interval "{interval}"')
def all_cases_line(context, diff, interval):
    assert f"(B minus A): {diff}, descriptive 95% interval {interval}" in context.compared, context.compared


@then('the comparison row "{group}" has the difference "{diff}", the p "{p}" and the mark "{mark}"')
def comparison_row(context, group, diff, p, mark):
    row = _comparison_rows(context)[group]
    assert row[4:7] == [diff, p, mark], row


@then("the two comparison tables are the same, line for line")
def same_tables(context):
    assert context.first_table.splitlines() == context.compared.splitlines()


@then("the report holds the printed comparison, line for line")
def report_holds_comparison(context):
    assert context.compared.strip() in _report(context)


@then('the printed output does not hold "{text}"')
def out_lacks(context, text):
    assert text not in context.printed


def _eval_row(context, cell) -> list[str]:
    section = _section(_report(context), "## Eval trip-sql")
    return {r[0]: r[1:] for r in _md_rows(section, 5)}[cell]


@then('the eval row "{cell}" is "{cells}"')
def eval_row(context, cell, cells):
    assert _eval_row(context, cell) == [c.strip() for c in cells.split("|")], _eval_row(context, cell)


@then('the eval row "{cell}" passes')
def eval_row_passes(context, cell):
    assert _eval_row(context, cell)[-1] == "passes", _eval_row(context, cell)


@then('the report has the comparisons "{names}"')
def report_comparisons(context, names):
    for name in names.split(", "):
        assert f"### {name}" in _report(context), name


@then('its row of the cost and time table is "{cells}"')
def cost_time_row(context, cells):
    row = _md_rows(context.cell_text, 6)[0]
    assert row[1:] == [c.strip() for c in cells.split("|")], row


def _cost_row(context, cell) -> list[str]:
    return {r[0]: r[1:] for r in _md_rows(_section(_report(context), "## Cost and time"), 6)}[cell]


@then('the cost and time row of "{cell}" is "{cells}"')
def cost_row_is(context, cell, cells):
    assert _cost_row(context, cell) == [c.strip() for c in cells.split("|")], _cost_row(context, cell)


@then('the cost and time row of "{cell}" has a CAD per answer above 0')
def cost_row_positive(context, cell):
    assert float(_cost_row(context, cell)[2]) > 0, _cost_row(context, cell)
