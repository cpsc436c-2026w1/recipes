"""Input and output checks around the main scenario (ext-guardrails-redteam/README.md).

Three places, each in app code, each with a verdict that goes into the trace and the red-team log:
1. input check, before the model sees the question: length and known injection phrasings;
2. own rows, inside the tool: the query runs in an engine that holds only the asking customer's rows of the
   tables in OWN_ROWS. Who may see whose rows is decided there, not in the system prompt and not by reading the
   SQL: a rule over the SQL text has to foresee every query, and `SELECT * FROM orders` got past the one that
   stood here before;
3. output check, before the user sees the reply: no part of the system prompt, no links, and every number of
   two or more digits in the reply must appear in the rows DuckDB returned.
The rules are short on purpose: the red-team log shows what they miss, and that count is the result.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from main.chat import Answer, answer
from main.prompts import SYSTEM_TEMPLATE

MAX_QUESTION_CHARS = 500  # ours: the longest real question in the normal set is under 100 characters

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules)",
    r"(system|developer) prompt",
    r"you are now",
    r"\bpretend\b",
    r"\breveal\b",
    r"\bjailbreak\b",
    r"(drop|truncate|alter) table",
]
REFUSAL = "I can only answer questions about the shop's sales."
OWN_ROWS = {"orders": "customer_id"}  # table -> the column that says whose row it is; a table not named is shared


@dataclass
class Verdict:
    ok: bool
    stage: str  # "input", "sql", "output", "error" (the model call failed: not tested) or "" when nothing stopped it
    reason: str = ""


def sql_that_ran(ans: Answer) -> list[str]:
    """Every query that passed the SQL check and ran for this answer, in order."""
    return [s.detail for s in ans.steps if s.kind == "tool_call" and s.status == "ok"]


def check_input(question: str) -> Verdict:
    if len(question) > MAX_QUESTION_CHARS:
        return Verdict(False, "input", f"longer than {MAX_QUESTION_CHARS} characters")
    for pat in INJECTION_PATTERNS:
        if re.search(pat, question, re.I):
            return Verdict(False, "input", f"matches /{pat}/")
    return Verdict(True, "")


_PROMPT_LINES = [ln.strip().lstrip("- ") for ln in SYSTEM_TEMPLATE.splitlines()
                 if len(ln.strip()) > 30 and "{" not in ln]


def check_output(reply: str, rows: list) -> Verdict:
    low = reply.lower()
    for line in _PROMPT_LINES:
        if line.lower() in low:
            return Verdict(False, "output", "reply repeats the system prompt")
    if re.search(r"https?://|www\.", low):
        return Verdict(False, "output", "reply holds a link")
    seen = {_num(v) for row in rows for v in row if isinstance(v, (int, float))}
    for n in re.findall(r"\d[\d,]*\.?\d*", reply):
        value = _num(n.replace(",", ""))
        if value is not None and abs(value) >= 10 and value not in seen:
            return Verdict(False, "output", f"number {n} is not in the query result")
    return Verdict(True, "")


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def guarded_answer(question: str, *, user_id: str, model, engine, data_version: str = "", traces=None):
    """Returns (answer, verdict). A refused question never reaches the model, so it does not use the budget."""
    v = check_input(question)
    if not v.ok:
        return _refused(question, user_id, v), v
    # Decision (access to other customers' rows): the query runs where only the asker's rows exist.
    ans = answer(question, user_id=user_id, model=model, engine=engine.for_rows_of(OWN_ROWS, user_id),
                 data_version=data_version, traces=None)
    rejected = [s for s in ans.steps if s.kind == "tool_call" and s.status == "rejected"]
    verdict = Verdict(True, "")
    if ans.status == "error":
        # The model call failed: no check refused anything and nothing passed either. The status stays "error",
        # the verdict says so and why; queries that passed the SQL check before the failure stay in ans.steps.
        verdict = Verdict(False, "error", ans.error)
    elif rejected and not ans.sql:
        verdict = Verdict(False, "sql", rejected[-1].detail.split(" -- ")[-1][:200])
        ans.status, ans.reply = "refused", REFUSAL
    elif ans.reply:  # every reply is checked, also one the model wrote without a query
        out = check_output(ans.reply, ans.rows)
        if not out.ok:
            verdict = out
            ans.status, ans.reply = "refused", REFUSAL
    if traces is not None:
        traces.put(ans)
    return ans, verdict


def _refused(question, user_id, v):
    return Answer(request_id=str(uuid.uuid4()), question=question, user_id=user_id, status="refused",
                  reply=REFUSAL, error=v.reason)
