"""Model calls: the course LLM gateway for real runs, a stub with fixed replies for tests.

Both have one method, `chat(messages, tools) -> ModelReply`, so the rest of the app does not know which one it has.

The gateway speaks the OpenAI chat format (recipes/llm-gateway). The request is written out in full below: a POST to
{COURSE_GATEWAY_URL}/chat/completions with your course MCP token as the bearer key. The gateway checks the key, the
per-key budget and the rate limit, and answers 429 when one is used up (README, "Decisions": model gateway).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Canadian dollars per million tokens (input, output): a copy of the course LLM gateway's price table for its two
# chat models (compared with the instructor's gateway settings on 2026-11-13). Stub replies are priced the same way.
PRICES_CAD = {
    "qwen3.8-27b": (0.10, 0.20),
    "qwen3.6-35b-a3b": (0.05, 0.10),
}


MAX_RATE_LIMIT_WAIT_S = 65  # the longest one wait for the rate limit (ours; the same as recipes/llm-gateway)
DEFAULT_RATE_LIMIT_WAIT_S = 5  # the wait when the refusal carries no Retry-After (ours; the same as llm-gateway)
RATE_LIMIT_CODES = ("rate_limit_exceeded", "upstream_rate_limited")  # the refusals that clear by waiting

# Decision (bounded work): an output-token cap on every model call. A reply here is a sentence and a SQL query, and 400
# tokens hold that. A model that reasons before it answers spends part of the cap on reasoning, so it gets more room:
# with a cap of 150, qwen3.8-27b's replies came back empty (finish_reason "length"); with 600, one answer of two
# sentences used 330 completion tokens, 277 of them reasoning
# (../../hybrid-retrieval-eval/output/routing-gateway-150-cap.txt). Add a model to the table when it does the same.
DEFAULT_MAX_TOKENS = 400
MAX_TOKENS_BY_MODEL = {"qwen3.8-27b": 600}
CAP_USED_UP = "the model used its token cap before answering"  # an empty reply that stopped at the cap (chat.py)


def max_tokens_for(model: str) -> int:
    return MAX_TOKENS_BY_MODEL.get(model, DEFAULT_MAX_TOKENS)


def cost_cad(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES_CAD.get(model, (0.0, 0.0))
    return (tokens_in * p_in + tokens_out * p_out) / 1e6


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    finish_reason: str = ""  # "stop", "tool_calls", or "length" when the reply reached max_tokens

    @property
    def cap_used_up(self) -> bool:
        """The reply stopped at max_tokens before it was whole: no text and no tool call, or a tool call cut in the
        middle of its arguments. Neither is an answer or a refusal, and a cut query is not run."""
        if self.finish_reason != "length":
            return False
        cut = any("_unparsed" in c.arguments for c in self.tool_calls)
        return cut or (not (self.content or "").strip() and not self.tool_calls)


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


class GatewayModel:
    """Calls the course LLM gateway. Needs COURSE_GATEWAY_URL and KB_MCP_TOKEN (main/config.py)."""

    def __init__(self, base_url: str, token: str, model: str, timeout_s: float = 250.0, max_tokens: int | None = None,
                 rate_limit_waits: int = 0, sleep=time.sleep):
        if not base_url or not token:
            raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, 'Run it')")
        self.model = model
        self.max_tokens = max_tokens or max_tokens_for(model)  # the cap of one call: MAX_TOKENS_BY_MODEL above
        # Decision (a failed call): 0 for the chat, where a person is waiting and a refusal is shown at once.
        # A batch run (ext-guardrails-redteam/redteam.py) sets 1: it waits for Retry-After and sends the call once
        # more, because a case that ends in a rate-limit refusal was not tested.
        self.rate_limit_waits = rate_limit_waits
        self.sleep = sleep
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s,
                                 headers={"Authorization": f"Bearer {token}"})

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, **sampling(self.model)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        r = self.http.post("/chat/completions", json=body)
        for _ in range(self.rate_limit_waits):
            if not _rate_limited(r):
                break
            self.sleep(min(_retry_after_s(r), MAX_RATE_LIMIT_WAIT_S))
            r = self.http.post("/chat/completions", json=body)
        if r.status_code in (401, 403, 429):
            # 401: missing or unknown key. 403: revoked or expired key, or a model this key may not use.
            # 429: code rate_limit_exceeded (wait for Retry-After) or budget_exhausted (your key's budget is spent).
            # None of them is retried here. The codes are the gateway's own: ../llm-gateway/ext-deploy/src/gateway/.
            raise RuntimeError(f"course LLM gateway refused the call: HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        calls = [ToolCall(id=c["id"], name=c["function"]["name"],
                          arguments=_parse_args(c["function"].get("arguments")))
                 for c in msg.get("tool_calls") or []]
        usage = data.get("usage") or {}
        return ModelReply(content=msg.get("content"), tool_calls=calls, model=data.get("model", self.model),
                          tokens_in=usage.get("prompt_tokens", 0), tokens_out=usage.get("completion_tokens", 0),
                          finish_reason=choice.get("finish_reason") or "")


def _rate_limited(r: httpx.Response) -> bool:
    """HTTP 429 with a code that clears by waiting: the key's own rate limit, or the model service's. 429 with
    budget_exhausted does not clear by waiting."""
    if r.status_code != 429:
        return False
    try:
        return (r.json().get("error") or {}).get("code") in RATE_LIMIT_CODES
    except ValueError:
        return False


def _retry_after_s(r: httpx.Response) -> float:
    value = r.headers.get("retry-after", "")
    return float(value) if value.isdigit() else DEFAULT_RATE_LIMIT_WAIT_S


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_unparsed": raw}


class StubModel:
    """Fixed replies for tests and offline runs: no network, no key, no cost.

    The script maps a question to what the model would do: {"sql": "..."} makes a tool call, {"reply": "..."}
    answers directly. "{customer_id}" in the SQL becomes the id the system prompt names. After a tool result the
    stub replies "The answer is {value}." with the first value of the first row, so the number in the reply comes
    from DuckDB, as in a real run; an entry's "final" replaces that sentence (the red team uses it). Token counts
    are estimates (characters / 4), not a tokenizer.
    """

    def __init__(self, script: dict[str, dict], model: str = "qwen3.6-35b-a3b"):
        self.script = {_norm(q): v for q, v in script.items()}
        self.model = model
        self.calls = 0

    @classmethod
    def from_file(cls, path: Path, **kw) -> "StubModel":
        return cls(json.loads(path.read_text()), **kw)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        self.calls += 1
        tokens_in = sum(len(json.dumps(m)) for m in messages) // 4
        question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        step = self.script.get(_norm(question), {"reply": "I can only answer questions about the shop's sales."})
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            if "error" in result:
                text = "I could not run a query for that question."
            elif result.get("rows"):
                text = step.get("final", "The answer is {value}.").format(value=result["rows"][0][0])
            else:
                text = "The query returned no rows."
            return ModelReply(content=text, tokens_in=tokens_in, tokens_out=len(text) // 4, model=self.model)
        if "sql" in step:
            # like a real model, the stub reads the asking customer's id from the system prompt
            found = re.search(r"customer_id '([^']+)'", messages[0]["content"])
            sql = step["sql"].replace("{customer_id}", found.group(1) if found else "")
            call = ToolCall(id=f"call-{self.calls}", name="run_sql", arguments={"sql": sql})
            return ModelReply(content=None, tool_calls=[call], tokens_in=tokens_in,
                              tokens_out=len(sql) // 4, model=self.model)
        return ModelReply(content=step["reply"], tokens_in=tokens_in, tokens_out=len(step["reply"]) // 4,
                          model=self.model)


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())
