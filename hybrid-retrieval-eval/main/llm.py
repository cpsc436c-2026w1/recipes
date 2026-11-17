"""Chat calls through the course LLM gateway, and a stub with fixed replies for tests.

Both have `chat(model, system, user, max_tokens) -> Reply`. The gateway call is the OpenAI chat format: a POST to
{COURSE_GATEWAY_URL}/chat/completions with your course MCP token as the bearer key.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# Canadian dollars per million tokens (input, output): a copy of the course LLM gateway's prices for these three
# models. A person still has to check them against the live gateway (README, "Cost and clean-up").
PRICES_CAD = {
    "qwen3.8-27b": (0.10, 0.20),
    "qwen3.6-35b-a3b": (0.05, 0.10),
    "qwen3-embedding-0.6b": (0.01, 0.01),
}


def cost_cad(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES_CAD[model]
    return (tokens_in * p_in + tokens_out * p_out) / 1e6


@dataclass
class Reply:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    finish_reason: str = ""   # "stop", or "length" when max_tokens cut the reply; "" when the endpoint gave none

    @property
    def cost_cad(self) -> float:
        return cost_cad(self.model, self.tokens_in, self.tokens_out)


# Decision (gateway refusals): a rate-limit refusal clears when the gateway's minute turns over, so waiting helps;
# a spent budget, a bad key or a model outside your list does not clear by waiting (recipes/llm-gateway, "Decisions":
# retries). The eval sends one chat call per query, so it waits out a rate-limit refusal, a limited number of times.
MAX_TRIES = 4        # ours (assumed): the first call and up to three more
MAX_WAIT_S = 65      # ours (assumed): one wait is never longer; a longer Retry-After stops the run instead
RETRY_CODES = ("rate_limit_exceeded", "upstream_rate_limited")


class GatewayRefused(RuntimeError):
    """The gateway answered with an error. `code` says which one (recipes/llm-gateway, "What you should see")."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}" if status else f"{code}: {message}")
        self.status, self.code = status, code


def post(http: httpx.Client, path: str, body: dict, sleep=time.sleep) -> dict:
    """POST to the gateway and return the parsed reply. A rate-limit refusal (HTTP 429) is waited out: for as long
    as the Retry-After header says, else 1, 2, 4 s; at most MAX_TRIES calls. Every other refusal is raised at once."""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            r = http.post(path, json=body)
        except httpx.TimeoutException as err:
            # A call that timed out may still be charged, so it is not sent again (GENREL03-BP01: fail gracefully)
            raise GatewayRefused(0, "no_answer", f"no answer within the client's time limit ({err})") from err
        except httpx.TransportError as err:   # no connection, a reset, a broken protocol: no answer at all
            raise GatewayRefused(0, "no_connection", f"{type(err).__name__}: {err}") from err
        if r.status_code < 400:
            try:
                d = r.json()
            except ValueError:
                d = None
            if not isinstance(d, dict) or "error" in d:
                raise GatewayRefused(r.status_code, "not_an_answer", f"status {r.status_code}, body {r.text[:200]!r}")
            return d
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        code, message = err.get("code") or "unknown", err.get("message") or r.text[:200]
        key = http.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if key and isinstance(message, str):
            message = message.replace(key, "<your course token>")   # R37: a refusal may echo what it was sent
        wait = _retry_after(r.headers.get("retry-after"), default=2.0 ** (attempt - 1))
        if r.status_code != 429 or code not in RETRY_CODES or attempt == MAX_TRIES or wait > MAX_WAIT_S:
            raise GatewayRefused(r.status_code, code, message)
        sleep(wait)
    raise AssertionError("unreachable")  # the loop returns or raises


def _retry_after(header: str | None, default: float, now=None) -> float:
    """Seconds to wait from a Retry-After header: a number of seconds, or an HTTP date (RFC 9110, section 10.2.3).
    The gateway forwards the upstream provider's header, so either form can arrive."""
    if header is None:
        return default
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


class GatewayChat:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None, sleep=time.sleep):
        if not base_url or not token:
            raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, 'Run it')")
        self.http = http or httpx.Client(base_url=base_url, timeout=250, headers={"Authorization": f"Bearer {token}"})
        self.sleep = sleep

    def chat(self, model: str, system: str, user: str, max_tokens: int = 300) -> Reply:
        d = post(self.http, "/chat/completions", {
            "model": model, "max_tokens": max_tokens, **sampling(model),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, self.sleep)
        u = d.get("usage") or {}
        choices = d.get("choices")
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise GatewayRefused(200, "not_a_chat_completion", f"no choices in {str(d)[:200]!r}")
        return Reply(choices[0]["message"].get("content") or "", model,
                     u.get("prompt_tokens", 0), u.get("completion_tokens", 0), choices[0].get("finish_reason") or "")


class StubChat:
    """Replies from a file: {model: {key: reply}}. The key is the user message's first line (the request id or the
    query). Token counts are estimates (characters / 4). A missing key gets `default`."""

    def __init__(self, replies: dict[str, dict[str, str]], default: str = ""):
        self.replies = replies
        self.default = default
        self.calls: list[tuple[str, str]] = []

    @classmethod
    def from_file(cls, path: Path, **kw) -> "StubChat":
        return cls(json.loads(path.read_text()), **kw)

    def chat(self, model: str, system: str, user: str, max_tokens: int = 300) -> Reply:
        key = user.splitlines()[0].strip()
        self.calls.append((model, key))
        text = self.replies.get(model, {}).get(key, self.default)
        return Reply(text, model, (len(system) + len(user)) // 4, max(1, len(text) // 4))
