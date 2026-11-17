"""Model calls for the three pattern files: the course LLM gateway for real runs, a stub for offline runs.

Both go through `Model.chat(system, user) -> str`, so a pattern file does not know which one it has. `Model` also
counts the calls and refuses the one over `max_calls`: every pattern here has a limit the model cannot remove.

Settings come from the environment or a `.env` file (see `.env.example`); nothing secret is written in code.
The gateway speaks the OpenAI chat format: POST {COURSE_GATEWAY_URL}/chat/completions, your course token as the key.
"""
import argparse
import threading
import time

import httpx
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    gateway_url: str = Field("", validation_alias=AliasChoices("COURSE_GATEWAY_URL", "GATEWAY_URL"))
    token: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("KB_MCP_TOKEN", "LLM_API_KEY", "COURSE_MCP_TOKEN"))
    model: str = Field("qwen3.8-27b", validation_alias="LLM_MODEL")


class CallCapReached(RuntimeError):
    """The run asked for one model call more than `max_calls`."""


class Model:
    """`stub` is a function (system, user) -> reply text; without one, calls go to the course LLM gateway."""

    def __init__(self, stub=None, max_calls: int = 6, settings: Settings | None = None, transport=None):
        self.stub, self.max_calls = stub, max_calls
        self.calls: list[dict] = []          # one entry per call: system, user, reply, start, end
        self.lock = threading.Lock()
        if stub is None:
            s = settings or Settings()
            if not s.gateway_url or not s.token.get_secret_value():
                raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, 'Run it')")
            self.name = s.model
            self.http = httpx.Client(base_url=s.gateway_url.rstrip("/"), timeout=250.0, transport=transport,
                                     headers={"Authorization": f"Bearer {s.token.get_secret_value()}"})

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        with self.lock:                      # the cap holds when calls come from several threads
            if len(self.calls) >= self.max_calls:
                raise CallCapReached(f"call cap reached: {self.max_calls} model calls")
            call = {"system": system, "user": user, "start": time.monotonic()}
            self.calls.append(call)
        call["reply"] = self.stub(system, user) if self.stub else self._gateway(system, user, temperature)
        call["end"] = time.monotonic()
        return call["reply"]

    def _gateway(self, system: str, user: str, temperature: float) -> str:
        # max_tokens 1500: qwen3.8-27b reasons before it answers, and the reasoning counts against the cap. One
        # one-word label took 546 output tokens, 541 of them reasoning; with a cap of 600 a vote came back empty.
        body = {"model": self.name, "max_tokens": 1500, "temperature": temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = self.http.post("/chat/completions", json=body)
        if r.status_code != 200:             # 401/403: the key; 429: rate limit or spent budget. Not retried.
            raise RuntimeError(f"course LLM gateway refused the call: HTTP {r.status_code} {r.text[:200]}")
        try:
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError(f"course LLM gateway sent a reply that is not a chat completion: {r.text[:120]!r}") from None
        if not text:
            raise RuntimeError("the model used its token cap before answering (empty reply)")
        return text


def model_from_args(stub, max_calls: int) -> Model:
    """`--model stub` (the default: no network, no key) or `--model gateway`; `--max-calls N` lowers or raises the cap."""
    p = argparse.ArgumentParser()
    p.add_argument("--model", choices=["stub", "gateway"], default="stub")
    p.add_argument("--max-calls", type=int, default=max_calls)
    a = p.parse_args()
    print(f"model: {a.model}; at most {a.max_calls} model calls")
    return Model(stub if a.model == "stub" else None, a.max_calls)
