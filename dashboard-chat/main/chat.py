"""One question, start to end: system prompt, model, tool call, SQL check, DuckDB, reply, one trace item.

README, "Main scenario", steps 2 to 6. The loop is bounded: at most MAX_MODEL_CALLS model calls per question
(README, "Decisions": bounded loop).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from main.engine import Engine
from main.model import CAP_USED_UP, ModelReply, cost_cad
from main.prompts import RUN_SQL_TOOL, system_prompt
from main.sqlcheck import SqlRejected, SqlRule, check_sql

MAX_MODEL_CALLS = 4  # one call that writes SQL, one retry after a rejected query, one reply, one spare (ours)
STEP_LIMIT_REPLY = "I could not answer within the step limit."


@dataclass
class Step:
    kind: str  # "model_call" or "tool_call"
    name: str  # model id, or the tool's name
    start_ms: float  # from the start of the request
    duration_ms: float
    status: str  # "ok", "rejected", "error"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cad: float = 0.0
    detail: str = ""  # the SQL of a tool call, or the rejection message
    truncated: str = ""  # what was cut from a tool call's result: "rows", "text", "rows and text", or ""


@dataclass
class Answer:
    request_id: str
    question: str
    user_id: str
    status: str  # "answered", "no_answer", "error"; extensions add "refused" and "cache_hit"
    reply: str = ""
    sql: str = ""  # the last SQL that ran
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # what the dashboard shows: the numbers come from DuckDB
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cad: float = 0.0
    latency_ms: float = 0.0
    data_version: str = ""
    steps: list[Step] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def answer(question: str, *, user_id: str, model, engine: Engine, data_version: str = "",
           extra_sql_rules: tuple[SqlRule, ...] = (), traces=None) -> Answer:
    t0 = time.monotonic()
    ans = Answer(request_id=str(uuid.uuid4()), question=question, user_id=user_id, status="no_answer",
                 data_version=data_version)
    messages = [
        {"role": "system", "content": system_prompt(engine.schema(), customer_id=user_id)},
        {"role": "user", "content": question},
    ]
    try:
        for n in range(1, MAX_MODEL_CALLS + 1):
            reply = _timed_model_call(model, messages, ans, t0)
            if reply.cap_used_up:
                # Decision (bounded work): an empty reply that stopped at max_tokens is neither an answer nor a
                # refusal. The question ends as an error that says so; the rows of a query that ran are taken back,
                # because no reply reads them.
                ans.status, ans.error, ans.reply = "error", CAP_USED_UP, ""
                ans.sql, ans.columns, ans.rows = "", [], []
                ans.steps[-1].status, ans.steps[-1].detail = "error", CAP_USED_UP
                break
            if not reply.tool_calls:
                ans.reply = (reply.content or "").strip()
                ans.status = "answered" if ans.sql else "no_answer"
                break
            if n == MAX_MODEL_CALLS:
                # Decision (bounded work): no model call is left to read a result, so this query is not run. The
                # rows of an earlier query are taken back too: a table next to "I could not answer" reads as an
                # answer. The queries that ran stay in the steps, for the trace.
                ans.reply = STEP_LIMIT_REPLY
                ans.sql, ans.columns, ans.rows = "", [], []
                break
            messages.append(_assistant_message(reply))
            for call in reply.tool_calls:
                result = _run_tool(call, engine, ans, t0, extra_sql_rules)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
    except Exception as e:  # a failed call ends this question, never the app; the trace records why
        ans.status, ans.error = "error", f"{type(e).__name__}: {e}"[:300]
    ans.latency_ms = round((time.monotonic() - t0) * 1000, 1)
    if traces is not None:
        traces.put(ans)
    return ans


def _timed_model_call(model, messages, ans: Answer, t0: float) -> ModelReply:
    start = time.monotonic()
    try:
        reply = model.chat(messages, tools=[RUN_SQL_TOOL])
    except Exception as e:
        ans.steps.append(Step("model_call", getattr(model, "model", "?"), _ms(t0, start), _ms(start), "error",
                              detail=str(e)[:200]))
        raise
    cost = cost_cad(reply.model, reply.tokens_in, reply.tokens_out)
    ans.steps.append(Step("model_call", reply.model, _ms(t0, start), _ms(start), "ok",
                          reply.tokens_in, reply.tokens_out, cost))
    ans.model = reply.model
    ans.tokens_in += reply.tokens_in
    ans.tokens_out += reply.tokens_out
    ans.cost_cad += cost
    return reply


def _run_tool(call, engine: Engine, ans: Answer, t0: float, extra_rules) -> dict:
    start = time.monotonic()
    sql = str(call.arguments.get("sql", ""))
    truncated = ""
    if call.name != "run_sql":
        result, status, detail = {"error": f"unknown tool {call.name}; the only tool is run_sql"}, "rejected", ""
    else:
        try:
            check_sql(sql, engine.tables, extra_rules)
            out = engine.run(sql)
            ans.sql, ans.columns, ans.rows = sql, out["columns"], out["rows"]
            result, status, detail, truncated = out, "ok", sql, out["truncated"] or ""
        except SqlRejected as e:
            result, status, detail = {"error": f"SQL check: {e}"}, "rejected", f"{sql} -- {e}"
        except Exception as e:  # DuckDB errors go back to the model so it can fix the query
            result, status, detail = {"error": f"DuckDB: {str(e).splitlines()[0][:200]}"}, "error", sql
    ans.steps.append(Step("tool_call", call.name, _ms(t0, start), _ms(start), status, detail=detail[:500],
                          truncated=truncated))
    return result


def _assistant_message(reply: ModelReply) -> dict:
    return {"role": "assistant", "content": reply.content,
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                           for c in reply.tool_calls]}


def _ms(t_from: float, t_to: float | None = None) -> float:
    """Milliseconds from t_from to t_to (or to now)."""
    end = time.monotonic() if t_to is None else t_to
    return round((end - t_from) * 1000, 2)
