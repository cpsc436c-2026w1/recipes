"""One question on Athena, start to end, within one deadline.

The loop is main/chat.py's (at most 4 model calls a question). This file adds what the extension needs around it:
- the rides prompt and a run_sql tool that says Athena;
- the question's deadline, 18 s from its start. Before each model call and each query the time left is checked; a
  model call's timeout is the smaller of 6 s and the time left; the query function gets the deadline itself;
- a backstop: httpx and botocore timeouts bound each wait, not the whole call (a reply trickled one byte at a time
  resets httpx's read timeout), so with a real clock the question runs in a worker thread that is joined with the
  time left. A worker left behind cannot change the answer returned or write a trace: the engine is closed for it
  and the trace is written here, once;
- the trace item gets each query's Data scanned, what Athena bills, the Athena cost in USD and the model calls' cost
  in CAD (R180).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

from athena_engine import AthenaEngine, Deadline, DeadlineReached
from main.chat import Answer, answer
from settings import Limits

SYSTEM_TEMPLATE = """You answer an operations analyst's questions about ride-hailing trips by writing Athena SQL
(Trino dialect).

Tables and columns:
{schema}

Rules:
- To answer, call the tool run_sql with one SELECT query over these tables only. Name the tables without a database.
- requested_day is the day a trip was requested, a date: filter one day with requested_day = DATE '2024-03-14'.
- pickup_zone_id joins zones.zone_id. Each row of trips is one trip; count trips with COUNT(*).
- After the tool result, reply in one or two sentences. Take every number from the tool result; do not compute
  numbers yourself.
- If the question is not about these tables, reply that you can only answer questions about the trips.
- The person asking has the id '{customer_id}'; the tables have no per-person rows."""

RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run one read-only Athena SELECT over the tables trips and zones and return the rows "
                       "(at most 50).",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One SELECT statement"}},
            "required": ["sql"],
        },
    },
}


def system_prompt(schema: dict, customer_id: str) -> str:
    lines = [f"- {t}({', '.join(f'{c} {ty}' for c, ty in cols)})" for t, cols in schema.items()]
    return SYSTEM_TEMPLATE.format(schema="\n".join(lines), customer_id=customer_id)


class RealClock:
    real = True

    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(max(0.0, s))


class DeadlineModel:
    """Wraps a model: no call starts after the deadline, and each call's timeout is min(6 s, time left).

    GatewayModel takes the timeout as request_timeout_s (main/model.py); a stand-in model reads it the same way.
    A call that times out at the deadline is the deadline, not a model error.
    """

    def __init__(self, inner, deadline: Deadline, limits: Limits):
        self.inner, self.deadline, self.limits = inner, deadline, limits
        self.model = getattr(inner, "model", "?")

    def chat(self, messages, tools=None):
        left = self.deadline.check("a model call")
        self.inner.request_timeout_s = min(self.limits.model_call_max_s, left)
        try:
            return self.inner.chat(messages, tools=tools)
        except Exception:
            if self.deadline.left() <= 0.05:
                self.deadline.reached = True
                raise DeadlineReached("the model call was cut off at the question's deadline") from None
            raise


@dataclass
class AthenaAnswer(Answer):
    reason: str = ""  # "deadline" for a question that stopped at its deadline
    queries: list = field(default_factory=list)
    data_scanned_bytes: int = 0
    billed_bytes: int = 0
    athena_usd: float = 0.0
    model_cad: float = 0.0


def ask(question: str, *, model, engine: AthenaEngine, user_id: str = "analyst", traces=None,
        limits: Limits = Limits(), clock=None, data_version: str = "") -> AthenaAnswer:
    clock = clock or RealClock()
    deadline = Deadline(clock, limits.deadline_s)
    view = engine.new_question(deadline)  # this question's counter and record; a worker left behind keeps it
    wrapped = DeadlineModel(model, deadline, limits)

    def one() -> Answer:
        return answer(question, user_id=user_id, model=wrapped, engine=view, data_version=data_version,
                      prompt=system_prompt, tool=RUN_SQL_TOOL, traces=None)

    if getattr(clock, "real", True):
        box: dict = {}
        worker = threading.Thread(target=lambda: box.setdefault("a", one()), daemon=True)
        worker.start()
        # The wait ends at the deadline; GRACE_S more lets a query the function stopped at the deadline, or a model
        # call cut off there, come back with what it billed, so the trace has it (the spec keeps 1 s for the reply).
        worker.join(max(0.0, deadline.left()) + limits.grace_s)
        base = box.get("a")
        if base is None:  # still waiting at the deadline: leave the worker behind
            deadline.reached = True
            base = Answer(request_id="", question=question, user_id=user_id, status="stopped",
                          data_version=data_version)
    else:
        base = one()
    queries = view.close()
    ans = AthenaAnswer(**{k: getattr(base, k) for k in Answer.__dataclass_fields__})
    if not ans.request_id:
        import uuid

        ans.request_id = str(uuid.uuid4())
    if deadline.reached or deadline.left() <= 0:
        # Decision (one deadline): what ran stays in the trace; the rows of a query are taken back, because no reply
        # reads them.
        ans.status, ans.reason, ans.reply = "stopped", "deadline", ""
        ans.sql, ans.columns, ans.rows = "", [], []
    ans.latency_ms = round(deadline.elapsed() * 1000, 1)
    usage = engine.usage(queries)
    ans.queries = usage.get("queries", [])
    ans.data_scanned_bytes = usage.get("data_scanned_bytes", 0)
    ans.billed_bytes = usage.get("billed_bytes", 0)
    ans.athena_usd = usage.get("athena_usd", 0.0)
    ans.model_cad = round(ans.cost_cad, 9)
    # Not a dataclass field, so not in the trace item: the typed rows of the last query that succeeded (the eval).
    ok = [q for q in queries if q.get("state") == "SUCCEEDED"]
    ans.last_rows = ok[-1].get("rows") if ok else None
    if traces is not None:
        traces.put(ans)
    return ans


def to_dict(ans: AthenaAnswer) -> dict:
    return asdict(ans)
