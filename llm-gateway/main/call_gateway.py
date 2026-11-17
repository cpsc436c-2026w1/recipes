"""Call the course LLM gateway with your course MCP token: list your models, send one chat, handle a refusal.

    export COURSE_GATEWAY_URL=<the base URL on the course page or in your team repo, ending in /v1>
    read -s KB_MCP_TOKEN && export KB_MCP_TOKEN
    uv run python main/call_gateway.py
    uv run python main/call_gateway.py --model qwen3.8-27b --ask "Name one AWS region in Canada."

The gateway speaks the OpenAI chat format, so every request below is a plain HTTP request you can copy. README.md,
"Decisions", says why each one is built the way it is; the decision is named in the comment beside it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass

import httpx

# Decision (model): the cheaper of the course's two chat models by default; change it with CHAT_MODEL or --model.
DEFAULT_MODEL = os.environ.get("CHAT_MODEL", "qwen3.6-35b-a3b")

# Decision (max_tokens): always send it. The gateway reserves the price of max_tokens output tokens before the call
# and settles to the real tokens after it. With no max_tokens it reserves 4,096, so a key near the end of its
# budget is refused sooner than it needs to be.
DEFAULT_MAX_TOKENS = 300

# Decision (retries): wait out a rate-limit refusal once, at most this long. Never retry a spent budget.
MAX_WAIT_S = 65

# Decision (no answer): the longest wait for the next bytes. The gateway waits up to 240 s for a thinking model, and a
# reply that is not streamed sends no byte until the model has finished, so the client waits a little longer.
READ_TIMEOUT_S = 250.0


def gateway_env() -> tuple[str, str]:
    """The gateway's address and your token from the environment: COURSE_GATEWAY_URL, else GATEWAY_URL; KB_MCP_TOKEN,
    else COURSE_MCP_TOKEN (the older names still work)."""
    url = os.environ.get("COURSE_GATEWAY_URL") or os.environ.get("GATEWAY_URL") or ""
    token = os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN") or ""
    return url.rstrip("/"), token


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


@dataclass
class Reply:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    request_id: str
    cost_micro: int | None = None   # what the gateway charged for this call, in millionths, when it says so
    finish_reason: str = ""         # "stop", or "length" when the reply reached max_tokens

    @property
    def cap_used_up(self) -> bool:
        """No text, because the reply stopped at max_tokens: not an answer (README, "Cost and clean-up")."""
        return self.finish_reason == "length" and not self.text.strip()


class GatewayRefused(Exception):
    """The gateway answered with an error. `code` says which one (README, "What you should see")."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class Gateway:
    def __init__(self, base_url: str, token: str, timeout_s: float = READ_TIMEOUT_S, transport=None,
                 sleep=time.sleep):
        # Decision (keys): the token comes from the environment and goes only into the Authorization header.
        # It is never printed, logged or written to a file.
        if not base_url or not token:
            raise SystemExit("Set COURSE_GATEWAY_URL and KB_MCP_TOKEN in the environment first (README, 'Run it').")
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport,
                                 headers={"Authorization": f"Bearer {token}"})
        self.sleep = sleep

    def models(self) -> list[str]:
        """GET /models: the models this key may call. The gateway answers it without calling a model."""
        r = self.http.get("/models")
        _raise_for_refusal(r)
        return [m["id"] for m in r.json()["data"]]

    def chat(self, messages: list[dict], model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> Reply:
        """POST /chat/completions, once. Raises GatewayRefused on any refusal."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, **sampling(model)}
        r = self.http.post("/chat/completions", json=body)
        _raise_for_refusal(r)
        data = r.json()
        usage = data.get("usage") or {}
        # A gateway may name what it charged for the call in a header (ext-deploy does, for a reply that is not
        # streamed). The course LLM gateway is a separate deployment and may not send it: then there is no cost line.
        cost = r.headers.get("x-gateway-cost-micro", "")
        choice = data["choices"][0]
        return Reply(text=choice["message"].get("content") or "", model=data.get("model", model),
                     tokens_in=usage.get("prompt_tokens", 0), tokens_out=usage.get("completion_tokens", 0),
                     request_id=r.headers.get("x-request-id", ""),
                     cost_micro=int(cost) if cost.isdigit() else None,
                     finish_reason=choice.get("finish_reason") or "")

    def chat_with_retry(self, messages: list[dict], **kw) -> Reply:
        """chat(), waiting out one rate-limit refusal. Every other refusal goes straight to the caller."""
        try:
            return self.chat(messages, **kw)
        except GatewayRefused as e:
            # Decision (retries): rate_limit_exceeded clears at the next minute, so waiting helps once.
            # budget_exhausted, a bad or revoked token, or a model outside your list does not clear by waiting.
            if e.code not in ("rate_limit_exceeded", "upstream_rate_limited"):
                raise
            self.sleep(min(e.retry_after or 5, MAX_WAIT_S))
            return self.chat(messages, **kw)


def _raise_for_refusal(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    retry = r.headers.get("retry-after")
    raise GatewayRefused(r.status_code, err.get("code") or "unknown", err.get("message") or r.text[:200],
                         float(retry) if retry and retry.isdigit() else None)


# What to tell a person for each refusal code the gateway sends (ext-deploy/src/gateway/auth.py, budget.py, app.py).
ADVICE = {
    "invalid_api_key": "the token is missing or unknown: check KB_MCP_TOKEN",
    "key_revoked": "this token no longer works on the gateway: ask the teaching team",
    "key_expired": "this token has passed its last day on the gateway",
    "model_not_allowed": "pick a model from the list above",
    "rate_limit_exceeded": "too many requests this minute: wait for the next minute",
    "budget_exhausted": "this request does not fit what is left of your budget: lower max_tokens, "
                        "or stop and ask the teaching team",
    "upstream_rate_limited": "the model service is busy: try again shortly",
    "upstream_error": "the model service failed: try again; if it repeats, tell the teaching team",
    "upstream_rejected": "the model refused a parameter of this request (the message says which): change the "
                         "request, do not resend it",
    "upstream_key_refused": "the model service refused the gateway's own key: tell the teaching team",
    # A gateway with per-model call allowances (the course LLM gateway; not ext-deploy) sends these two.
    "model_allowance_used": "this model's calls for your key are used up: pick another model",
    "model_allowance_ended": "this model's allowance for your key has ended: pick another model",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ask", default="Reply with the word ready.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)
    gw = Gateway(*gateway_env())

    print("1. models this key may call")
    try:
        print("   " + ", ".join(gw.models()))
        print(f"2. one chat on {args.model}, max_tokens {args.max_tokens}")
        reply = gw.chat_with_retry([{"role": "user", "content": args.ask}], model=args.model,
                                   max_tokens=args.max_tokens)
    except GatewayRefused as e:
        print(f"   refused: HTTP {e.status} {e.code}")
        print(f"   {e.message}")
        print(f"   what to do: {ADVICE.get(e.code, 'read the message above')}")
        return 1
    except httpx.HTTPError as e:
        # No answer at all: a wrong address, no network, or no byte from the gateway for the time limit. Not tried again here:
        # the person at the terminal should see it at once, and a chat that timed out may still have been charged.
        print(f"   no answer from the gateway: {type(e).__name__}")
        print("   what to do: check COURSE_GATEWAY_URL (it ends in /v1) and your network, then run it again")
        return 1
    except ValueError:
        # An answer that is not JSON: often some other server answers at that address (a login page, a proxy's
        # error). The client cannot tell who sent it, so the line says "may".
        print("   the answer was not JSON, so it may not have come from the gateway")
        print("   what to do: check COURSE_GATEWAY_URL (it ends in /v1): another server may answer at that address")
        return 1
    if reply.cap_used_up:
        # Decision (max_tokens): an empty reply that stopped at the cap is an error, not an answer. A model that
        # reasons before it answers (qwen3.8-27b) can spend the whole cap on reasoning.
        print("   error: the model used its token cap before answering (finish_reason length)")
    else:
        print(f"   reply: {reply.text!r}")
    print(f"   tokens: {reply.tokens_in} in, {reply.tokens_out} out; request id {reply.request_id}")
    if reply.cost_micro is not None:
        print(f"   charged to your key: {reply.cost_micro / 1_000_000:.6f} in your key's currency "
              "(the gateway's own count for this call)")
    if reply.cap_used_up:
        print(f"   what to do: run it again with a larger --max-tokens (600 for qwen3.8-27b)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
