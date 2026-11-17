"""A stand-in for the course LLM gateway's limits: a budget per key and a rate per minute, enforced outside the app.

It mirrors the gateway in recipes/llm-gateway: before a call it reserves the highest price the call can reach
(input tokens plus max_tokens of output) and refuses with 429 `budget_exhausted` when that does not fit; after the
call it settles to the real price. It counts requests per key per minute and refuses with 429 `rate_limited` above
the limit. The course gateway has the same kind of per-key limits: the key's budget and the key's rate limit. The
defaults below are this stand-in's own example values, not the course gateway's.

The model behind it is a stub: `runaway` always asks for another tool call; `finishes_after(n)` replies after n.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import cost_cad


class GatewayRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"429 {code}: {message}")
        self.code = code


@dataclass
class ModelResult:
    tool_call: dict | None
    reply: str
    tokens_in: int
    tokens_out: int
    cost_cad: float


def runaway(step: int) -> dict | None:
    return {"name": "search", "arguments": {"q": f"try {step}"}}


def finishes_after(n: int):
    return lambda step: {"name": "search", "arguments": {"q": f"try {step}"}} if step < n else None


class FakeGateway:
    def __init__(self, behaviour, *, budget_cad: float = 1.0, rate_per_min: int = 60, clock=None,
                 call_s: float = 1.0, model: str = "qwen3.6-35b-a3b"):
        self.behaviour = behaviour
        self.budget = budget_cad
        self.spent = 0.0
        self.rate = rate_per_min
        self.clock = clock
        self.call_s = call_s  # each call advances the fake clock by this much, as a real call takes time
        self.model = model
        self.minute_counts: dict[int, int] = {}
        self.calls = 0

    def __call__(self, context_tokens: int, max_tokens: int) -> ModelResult:
        minute = int(self.clock() // 60) if self.clock else 0
        if self.minute_counts.get(minute, 0) >= self.rate:
            raise GatewayRefused("rate_limited", f"more than {self.rate} requests this minute")
        self.minute_counts[minute] = self.minute_counts.get(minute, 0) + 1
        reserve = cost_cad(self.model, context_tokens, max_tokens)
        if self.spent + reserve > self.budget:
            raise GatewayRefused("budget_exhausted", f"{self.spent:.4f} of {self.budget:.4f} CAD used")
        if self.clock and hasattr(self.clock, "advance"):
            self.clock.advance(self.call_s)
        call = self.behaviour(self.calls)
        self.calls += 1
        tokens_out = 30 if call else 60
        price = cost_cad(self.model, context_tokens, tokens_out)
        self.spent += price  # settle: the real price, at most the reserve
        return ModelResult(call, "" if call else "done", context_tokens, tokens_out, price)
