"""Model calls: the course LLM gateway for real runs, a stub with fixed replies for tests.

Both have one method, `chat(model, system, user, max_tokens) -> Reply`, so the router and the paths do not know
which one they have. Every reply carries its token counts and its cost, so the app can add up what a request cost.

The gateway speaks the OpenAI chat format (../llm-gateway): a POST to {COURSE_GATEWAY_URL}/chat/completions with your
course MCP token as the bearer key. It checks the key, the per-key budget and the rate limit, and answers 429 when
one is used up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

# Canadian dollars per million tokens (input, output): a copy of the course LLM gateway's price table for its two
# chat models (compared with the instructor's gateway settings on 2026-11-13). Stub replies are priced the same
# way, from estimated token counts.
PRICES_CAD = {
    "qwen3.8-27b": (0.10, 0.20),
    "qwen3.6-35b-a3b": (0.05, 0.10),
}


def cost_cad(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES_CAD.get(model, (0.0, 0.0))
    return (tokens_in * p_in + tokens_out * p_out) / 1e6


@dataclass
class Reply:
    text: str
    model: str
    tokens_in: int
    tokens_out: int

    @property
    def cost_cad(self) -> float:
        return cost_cad(self.model, self.tokens_in, self.tokens_out)


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


class GatewayModel:
    """Calls the course LLM gateway. Needs COURSE_GATEWAY_URL and KB_MCP_TOKEN (main/config.py)."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 250.0):  # longer than the gateway's 240 s
        if not base_url or not token:
            raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, 'Run it')")
        self.http = httpx.Client(base_url=base_url, timeout=timeout_s,
                                 headers={"Authorization": f"Bearer {token}"})

    def chat(self, model: str, system: str, user: str, max_tokens: int) -> Reply:
        body = {"model": model, "max_tokens": max_tokens, **sampling(model),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = self.http.post("/chat/completions", json=body)
        if r.status_code in (401, 403, 429):
            # 401: a missing or unknown key. 403: a revoked or expired key, or a model the key may not use.
            # 429: the rate limit (it clears at the next minute) or a spent budget; the error's `code` says which
            # (../llm-gateway/README.md). Not retried here: a retry on a spent budget only fails again.
            raise RuntimeError(f"course LLM gateway refused the call: HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        # A 200 is not yet an answer: a proxy's error page, an {"error": ...} body or an empty list of choices
        # also come with status 200. Each is a failed call, named as one, not a KeyError somewhere in the app.
        try:
            data = r.json()
            text = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
            tokens_in, tokens_out = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise RuntimeError(f"course LLM gateway sent a reply that is not a chat completion "
                               f"({type(e).__name__}): {r.text[:120]!r}") from None
        # model: the id this call asked for, not the reply's "model" field. An endpoint may answer with another
        # spelling of the name; the price table and the count of large-model calls both go by the id we sent.
        return Reply(text=text, model=model, tokens_in=tokens_in, tokens_out=tokens_out)


class StubModel:
    """Fixed replies for tests and offline runs: no network, no key, no cost.

    `router` maps a request (any case, any spacing) to the label the stub router returns; a request it does not
    know gets "hard". Any other call gets a short reply naming the model, so the output shows which model answered.
    Token counts are estimates (characters / 4), not a tokenizer.
    """

    def __init__(self, router: dict[str, str], router_system: str):
        self.router = {_norm(k): v for k, v in router.items()}
        self.router_system = router_system
        self.calls: list[str] = []          # model name per call, so tests can count large-model calls

    @classmethod
    def from_file(cls, path: Path, router_system: str) -> "StubModel":
        return cls(json.loads(path.read_text())["router"], router_system)

    def chat(self, model: str, system: str, user: str, max_tokens: int) -> Reply:
        self.calls.append(model)
        tokens_in = (len(system) + len(user)) // 4
        if system == self.router_system:
            text = self.router.get(_norm(user), "hard")
        else:
            text = f"(stub reply from {model}) " + user.splitlines()[-1][:120]
        tokens_out = min(max_tokens, max(1, len(text) // 4))
        return Reply(text=text, model=model, tokens_in=tokens_in, tokens_out=tokens_out)


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())
