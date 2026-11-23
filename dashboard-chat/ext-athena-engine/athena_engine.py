"""The engine of ext-athena-engine: the same contract as main/engine.py (tables, schema(), run(sql) -> columns, rows,
truncated), with each query sent to the Lambda function <PREFIX>-chat-query, which runs it on Athena.

Limits held here, outside the model (README, "Decisions"):
- at most 3 queries a question, also when one reply of the model asks for more (R160);
- no query starts once the question's deadline has passed, and the function gets the deadline itself (R210);
- what each query scanned and billed is kept, for the question's trace item (R180).
"""

from __future__ import annotations

import json
import threading
import time

from main.chat import ToolRefused
from settings import QUERY_LIMIT_RESULT, REGION, SCHEMA, TABLES, Limits, athena_usd


class DeadlineReached(Exception):
    """The question reached its deadline; chat_athena.ask ends it as status "stopped", reason "deadline"."""


class Deadline:
    """One absolute deadline for a question, on a clock with now() and sleep()."""

    def __init__(self, clock, seconds: float):
        self.clock = clock
        self.start = clock.now()
        self.at = self.start + seconds
        self.reached = False

    def left(self) -> float:
        return self.at - self.clock.now()

    def elapsed(self) -> float:
        return self.clock.now() - self.start

    def check(self, what: str) -> float:
        """The time left; DeadlineReached (and the flag) when none is."""
        left = self.left()
        if left <= 0:
            self.reached = True
            raise DeadlineReached(f"deadline reached before {what}")
        return left


class AthenaEngine:
    name = "Athena"  # the prefix of an engine error in the tool result (main/chat.py)
    dialect = "athena"  # what main/sqlcheck.py parses the SQL as

    def __init__(self, invoke, *, limits: Limits = Limits(), schema=None, tables=TABLES):
        self.invoke = invoke  # invoke(payload, timeout_s) -> query_fn's result
        self.limits = limits
        self._schema = schema or SCHEMA
        self.tables = tuple(tables)
        self._current = Question(self, None)

    def schema(self):
        return {t: list(cols) for t, cols in self._schema.items() if t in self.tables}

    def new_question(self, deadline: Deadline) -> "Question":
        """A view of this engine for one question, with its own counter, deadline and record of queries. A worker
        left behind at the deadline keeps the view of its own question, so it cannot use the next one's."""
        self._current = Question(self, deadline)
        return self._current

    # The current question, for callers that use the engine itself (main/chat.py's contract).
    @property
    def queries(self) -> list[dict]:
        return self._current.queries

    @property
    def deadline(self):
        return self._current.deadline

    def close_question(self) -> list[dict]:
        return self._current.close()

    def run(self, sql: str) -> dict:
        return self._current.run(sql)

    def usage(self, queries: list[dict] | None = None) -> dict:
        """What a question's queries scanned and billed (R180); queries: a snapshot from close_question."""
        qs = [q for q in (self.queries if queries is None else queries) if q["ran"]]
        billed = sum(q["billed_bytes"] for q in qs)
        return {"queries": [{k: q.get(k) for k in ("sql", "state", "query_execution_id", "data_scanned_bytes",
                                                   "billed_bytes")} for q in qs],
                "data_scanned_bytes": sum(q["data_scanned_bytes"] for q in qs), "billed_bytes": billed,
                "athena_usd": round(athena_usd(billed), 9)}


class LambdaInvoker:
    """Invokes <PREFIX>-chat-query. The client is built per call, so its timeouts are the time left plus 1 s, and
    it tries once: a retried invocation would run the query twice."""

    def __init__(self, function_name: str, region: str = REGION, session=None):
        self.function_name = function_name
        self.region = region
        self.session = session

    def __call__(self, payload: dict, timeout_s: float) -> dict:
        import boto3
        from botocore.config import Config

        t = max(1.0, timeout_s)
        client = (self.session or boto3).client(
            "lambda", region_name=self.region,
            config=Config(connect_timeout=t, read_timeout=t, retries={"total_max_attempts": 1}))
        r = client.invoke(FunctionName=self.function_name, Payload=json.dumps(payload).encode())
        body = json.loads(r["Payload"].read() or b"{}")
        if r.get("FunctionError"):
            return {"state": "FAILED", "message": f"the function failed: {body.get('errorMessage', '')[:200]}"}
        return body


class LocalInvoker:
    """Runs query_fn.run_query in this process, against a stand-in for Athena and a DynamoDB client (moto
    offline). It is how the offline examples go through the same function code as a live run."""

    def __init__(self, athena, usage, env: dict, clock, limits: Limits = Limits()):
        self.athena, self.usage, self.env, self.clock, self.limits = athena, usage, env, clock, limits
        self.calls: list[dict] = []

    def __call__(self, payload: dict, timeout_s: float) -> dict:
        from query_fn import run_query

        self.calls.append({"payload": payload, "timeout_s": timeout_s, "at": self.clock.now()})
        return run_query(payload, athena=self.athena, usage=self.usage, env=self.env, clock=self.clock,
                         poll_s=self.limits.poll_s, query_max_s=self.limits.query_max_s)


class Question:
    """One question's use of the engine: the query counter, the deadline, what each query scanned and billed."""

    def __init__(self, engine: AthenaEngine, deadline: Deadline | None):
        self.engine = engine
        self.name, self.dialect, self.tables = engine.name, engine.dialect, engine.tables
        self.limits = engine.limits
        self.deadline = deadline
        self.queries: list[dict] = []
        self._closed = False
        self._lock = threading.Lock()

    def schema(self):
        return self.engine.schema()

    def close(self) -> list[dict]:
        """No more queries for this question (a worker left behind at the deadline cannot add any)."""
        with self._lock:
            self._closed = True
            return [dict(q) for q in self.queries]

    def usage(self, queries: list[dict] | None = None) -> dict:
        return self.engine.usage(self.queries if queries is None else queries)

    def run(self, sql: str) -> dict:
        with self._lock:
            if self._closed:
                raise DeadlineReached("the question has ended")
            if sum(q["ran"] for q in self.queries) >= self.limits.max_queries:
                # Decision (bounded work): the query counter, not the cap of model calls, bounds the queries.
                raise ToolRefused(QUERY_LIMIT_RESULT)
            left = self.deadline.check("a query") if self.deadline else self.limits.query_max_s
            record = {"sql": sql, "ran": True, "state": "INVOKED", "data_scanned_bytes": 0, "billed_bytes": 0}
            self.queries.append(record)
        payload = {"sql": sql, "deadline": self.deadline.at if self.deadline else time.time() + left}
        out = self.engine.invoke(payload, left + 1.0)  # the function's own wait on the deadline ends the call
        with self._lock:
            record.update(state=out["state"], data_scanned_bytes=out.get("data_scanned_bytes", 0),
                          billed_bytes=out.get("billed_bytes", 0),
                          query_execution_id=out.get("query_execution_id", ""),
                          workgroup=out.get("workgroup", ""), database=out.get("database", ""))
        state = out["state"]
        if state in ("REFUSED", "DENIED"):
            record["ran"] = False  # refused by the daily cap or by the role: nothing ran on Athena
            raise ToolRefused(out["message"])
        if state == "CUTOFF":
            raise ToolRefused(out["message"])
        if state == "STOPPED":
            if out["message"] == "deadline" or (self.deadline and self.deadline.left() <= 0):
                self.deadline.reached = True
                raise DeadlineReached("the query was stopped at the question's deadline")
            raise RuntimeError(out["message"])
        if state != "SUCCEEDED":
            raise RuntimeError(out["message"] or state)
        record.update(columns=out["columns"], types=out["types"], rows=out["rows"])
        return {"columns": out["columns"], "rows": out["rows"], "truncated": out["truncated"]}
