"""Model access: the course LLM gateway or the offline stub, both behind one spend cap.

Decision (README, Decisions: "Calls through the course gateway"): every model call goes to the course LLM
gateway, an OpenAI-compatible endpoint, with your course MCP token as the API key. The gateway keeps the provider
key and enforces your per-key budget and rate; the cap here stops this program well before that budget.

Decision (README, Decisions: "Spend cap in code"): `Budget` counts every call by role and refuses a call past its
cap. GEPA also gets `max_metric_calls` (its own budget of task-model calls) and `Budget.stop` (stops the search
when the reflection cap or the money cap is reached), so the hard refusal is a backstop. GEPA and DSPy catch an
exception raised inside an evaluation and go on (gepa/core/engine.py, `except Exception` in the main loop;
dspy/teleprompt/gepa/gepa_utils.py scores a failed example 0), so a refused or failed call is also recorded on the
`Budget`, and `Budget.stop` then ends the search: the loop checks it before every iteration. After that, no
reflection call is made: `reserve` refuses it, so no paid reflection rewrites a prompt from refused answers.

The search's task-model calls and the test runs' calls are counted apart (`max_task_calls`, `max_test_calls`), so a
search that ends at its cap still leaves the test runs their own allowance. The money cap is one for the whole run.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from dataclasses import dataclass, field

import dspy
from dspy.lm15 import Message, Response, TextPart, Usage

from main import stub

# Canadian dollars per million tokens (input, output): a copy of the course LLM gateway's price table for its two
# chat models (compared with the instructor's gateway settings on 2026-11-13). The stub models are priced as the
# gateway model they stand for.
PRICES_CAD = {
    "qwen3.6-35b-a3b": (0.05, 0.10),
    "qwen3.8-27b": (0.10, 0.20),
    "stub-small": (0.05, 0.10),
    "stub-large": (0.10, 0.20),
}

# Task model: the smaller, cheaper model, whose prompt has the most to gain. Reflection model: the larger one;
# DSPy's GEPA docs ask for a strong reflection model (README, Decisions: "Two models").
TASK_MODEL = "qwen3.6-35b-a3b"
REFLECTION_MODEL = "qwen3.8-27b"
STUB_FOR = {TASK_MODEL: "stub-small", REFLECTION_MODEL: "stub-large"}

# qwen3.8-27b is a reasoning model and spends output tokens on reasoning before the reply (../llm-probes);
# a small max_tokens leaves the reply empty. 4000 is assumed enough for one SQL query.
TASK_MAX_TOKENS = 4000
# A reflection reply is a whole instruction text, written after the model has reasoned over every failure. With a
# thinking reflection model, a cap of about 4,000 left some replies empty. REFLECTION_MAX_TOKENS changes it. A
# gateway may clamp a larger request to its own per-key cap without an error (the gateway in ../llm-gateway clamps
# to the key's max_tokens_cap, 16,384 as shipped), so a key with a lower cap gives the reply less room than this.
REFLECTION_MAX_TOKENS = int(os.environ.get("REFLECTION_MAX_TOKENS", "16384"))


def max_tokens_for(role: str) -> int:
    """The output cap of one call in `role`: the reflection model gets room to reason, the task model does not."""
    return REFLECTION_MAX_TOKENS if role == "reflect" else TASK_MAX_TOKENS


def is_gpt(model: str) -> bool:
    """OpenAI GPT and o-series names. They accept only their default temperature, 1."""
    return bool(re.match(r"(openai/)?(gpt-|o\d)", model.lower()))


class EmptyReflection(RuntimeError):
    """A reflection reply with no text: the model spent its whole cap on reasoning (finish_reason "length")."""


class BudgetExceeded(RuntimeError):
    pass


def cad_text(amount: float) -> str:
    """A cap as text: two decimals from one cent up, four below it, so that a small cap does not print as 0.00."""
    return f"{amount:.2f}" if amount >= 0.01 else f"{amount:.4f}" if amount >= 0.0001 else f"{amount:.6f}"


class ModelCallFailed(RuntimeError):
    """The endpoint refused or failed a call (bad token, rate limit, spent key budget, network). Not a wrong answer."""


@dataclass
class Budget:
    """Calls and Canadian dollars spent, per role ("task", "reflect", "test-before", ...)."""

    max_reflection_calls: int = 10
    max_task_calls: int = 400    # task-model calls of the search (and of any role that is not "reflect" or "test-...")
    max_test_calls: int = 400    # calls of the test runs (roles "test-..."), not counted in max_task_calls
    max_cad: float = 1.00
    search_share: float = 0.6
    calls: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)   # role -> [tokens in, tokens out]
    cad: dict = field(default_factory=dict)
    stopped_by: str = ""     # why the search was told to stop: a cap that was reached, or a refused call
    failed: str = ""         # the first model call that failed for a reason other than a cap
    cut_at: int = 0          # the number of the model call (all roles) at which the search was cut, 0 if it was not
    held: float = 0.0        # CAD reserved for calls that are in flight: the most each of them can cost
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def total_cad(self) -> float:
        return sum(self.cad.values())

    def reserve(self, role: str, model: str | None = None, prompt_bytes: int = 0) -> float:
        """Called before every model call. Raises BudgetExceeded instead of making a call past a cap.

        Decision (README, Decisions: "Spend cap in code"): the money cap is checked against what is spent, what
        the calls in flight can still cost, and the most this call can cost: one input token per byte of the
        prompt (a token is at least one byte) and the role's output cap (`max_tokens_for`), at the model's prices. That amount is
        held until `settle` or `fail` gives it back. Without the hold, four threads each pass the check before
        any of them has been charged, and the run ends above the cap. Returns the amount held.
        """
        worst = 0.0
        if model is not None:
            p_in, p_out = PRICES_CAD[model]
            worst = (prompt_bytes * p_in + max_tokens_for(role) * p_out) / 1e6
        with self._lock:
            test = role.startswith("test-")
            if role != "reflect" and not test and (self.failed or self.cut_at):
                # The search was cut by a failed or refused call. The other examples of the evaluation that was
                # running are not sent: their answers would be paid for and never used.
                raise BudgetExceeded(f"{role}: the search was cut ({self.failed or self.stopped_by})")
            refusal = ""
            if role == "reflect":
                n, cap = self.calls.get("reflect", 0), self.max_reflection_calls
                if self.stopped_by or self.failed:
                    # The search has stopped: a reflection now would rewrite a prompt from answers that were
                    # refused or never given, and GEPA would pay for it before it checks stop() again.
                    refusal = f"reflect: the search has stopped ({self.stopped_by or self.failed})"
            elif test:
                n, cap = sum(v for r, v in self.calls.items() if r.startswith("test-")), self.max_test_calls
            else:
                n = sum(v for r, v in self.calls.items() if r != "reflect" and not r.startswith("test-"))
                cap = self.max_task_calls
            if not refusal and n >= cap:
                refusal = f"{role}: {n} calls reached the cap of {cap}"
            elif not refusal and (self.total_cad() >= self.max_cad
                                  or self.total_cad() + self.held + worst > self.max_cad):
                refusal = (f"spent {self.total_cad():.6f} CAD, {self.held:.6f} held for calls in flight, "
                           f"this call can cost up to {worst:.6f}, cap {cad_text(self.max_cad)}")
            if refusal:
                if not test and not self.stopped_by:
                    # Recorded as well as raised: GEPA and DSPy catch the exception, and stop() reads this.
                    self.stopped_by = f"spend cap, call refused ({refusal})"
                    self.cut_at = self.cut_at or sum(self.calls.values()) + 1
                raise BudgetExceeded(refusal)
            self.calls[role] = self.calls.get(role, 0) + 1
            self.held += worst
        return worst

    def fail(self, role: str, err: Exception, held: float = 0.0) -> "ModelCallFailed":
        """Record a call that the endpoint refused or that broke; returns the exception to raise."""
        text = f"{role}: {type(err).__name__}: {err}"
        with self._lock:
            self.held = max(self.held - held, 0.0)
            if not self.failed:
                self.failed = text
                if not role.startswith("test-"):
                    self.cut_at = self.cut_at or sum(self.calls.values())  # this call was counted by reserve()
        return ModelCallFailed(text)

    def settle(self, role: str, model: str, t_in: int, t_out: int, held: float = 0.0) -> None:
        p_in, p_out = PRICES_CAD[model]
        with self._lock:
            self.held = max(self.held - held, 0.0)
            t = self.tokens.setdefault(role, [0, 0])
            t[0] += t_in
            t[1] += t_out
            self.cad[role] = self.cad.get(role, 0.0) + (t_in * p_in + t_out * p_out) / 1e6

    def stop(self, gepa_state=None) -> bool:
        """GEPA stop callback: True ends the search before the next iteration.

        The search stops at `search_share` of the money cap, so the test runs still fit under the cap
        (the L15 demo stopped GEPA at US$3.00 under a US$4.00 hard cap).
        """
        with self._lock:
            if not self.stopped_by:
                if self.calls.get("reflect", 0) >= self.max_reflection_calls:
                    self.stopped_by = f"reflection cap ({self.max_reflection_calls} calls)"
                elif self.total_cad() >= self.search_share * self.max_cad:
                    self.stopped_by = (f"money cap (search stops at {self.search_share:.0%} of "
                                       f"{cad_text(self.max_cad)} CAD)")
            # A refused call (reserve) or a failed call (fail) also ends the search: GEPA would otherwise catch
            # the exception and start the next iteration, which is refused again, without end.
            return bool(self.stopped_by or self.failed)


class _Capped:
    """Mixin for a DSPy LM: reserve before the call, settle the tokens after it."""

    budget: Budget
    role: str
    price_as: str

    def __call__(self, prompt=None, **kwargs):
        # The prompt's size in bytes, as an upper bound on its input tokens. repr() of a Request holds every
        # message's text and some more, so it errs on the high side.
        size = len((prompt if isinstance(prompt, str) else repr((prompt, kwargs.get("messages")))).encode())
        held = self.budget.reserve(self.role, self.price_as, size)
        try:
            out = super().__call__(prompt, **kwargs)
        except Exception as err:  # the endpoint refused the call or the call broke: not an answer to score
            raise self.budget.fail(self.role, err, held) from err
        if isinstance(out, Response) and out.usage is not None:
            t_in, t_out = out.usage.input_tokens or 0, out.usage.output_tokens or 0
        else:  # a string prompt (the reflection call) returns a list; its usage is in the history entry
            u = (self.history[-1].get("usage") if self.history else None) or {}
            t_in = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
            t_out = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
        self.budget.settle(self.role, self.price_as, t_in, t_out, held)
        if self.role == "reflect" and not _reply_text(out).strip():
            # Not an instruction text: GEPA would take the empty reply as the next prompt. A failed call instead,
            # named in the report, so the cap can be raised (REFLECTION_MAX_TOKENS) before the next run.
            raise self.budget.fail(self.role, EmptyReflection(
                f"empty reply with {t_out} output tokens: the reflection model used its cap of "
                f"{max_tokens_for(self.role)} before it wrote the instruction (raise REFLECTION_MAX_TOKENS)"))
        return out


def _reply_text(out) -> str:
    """The text of an LM's reply: a Response (called with messages, as the adapters do), or the list of outputs of
    a call with a string prompt (strings, or dicts with "text")."""
    if isinstance(out, Response):
        parts = out.message.parts if out.message is not None else []
        return "".join(p.text for p in parts if isinstance(p, TextPart))
    if isinstance(out, list):
        return "".join(o if isinstance(o, str) else (o.get("text") or "") if isinstance(o, dict) else ""
                       for o in out)
    return str(out or "")


class GatewayLM(_Capped, dspy.LM):
    """A DSPy LM for one model on the course LLM gateway."""

    def __init__(self, model: str, role: str, budget: Budget):
        base_url, token = gateway_settings()
        # model_type="chat": the gateway serves /chat/completions only, and DSPy 3.5 picks OpenAI's Responses API
        # for "openai/..." models by default (model_type "auto"). cache=False: repeated test runs must be real
        # calls, since replies vary. A GPT or o-series model gets temperature 1.0, the only value it accepts (DSPy
        # also refuses max_tokens under 16,000 for its gpt-5 and o-series names); every other model gets none.
        # num_retries=0: one call is one request, as in gateway_chat. A retry would send the request again under
        # the one hold of `reserve`, and the gateway charges a sent request that times out or gets a 5xx its
        # whole reserve: three attempts could cost the key three times what the cap here counted.
        super().__init__(f"openai/{model}", model_type="chat", api_base=base_url, api_key=token,
                         temperature=1.0 if is_gpt(model) else None, max_tokens=max_tokens_for(role),
                         cache=False, num_retries=0)
        self.budget, self.role, self.price_as = budget, role, model


class _StubEngine:
    """A DSPy engine (complete(Request) -> Response) that answers with main/stub.py instead of a model."""

    def __init__(self, stub_model: str, kind: str):
        self.stub_model, self.kind = stub_model, kind

    def complete(self, request):
        text = lambda parts: "".join(p.text for p in parts if isinstance(p, TextPart))  # noqa: E731
        system = request.system if isinstance(request.system, str) else text(request.system or [])
        user = "\n".join(text(m.parts) for m in request.messages if m.role == "user")
        if self.kind == "reflect":
            out = stub.reflect_reply(user)
        else:
            # The reply in DSPy's ChatAdapter format: one marker per output field.
            out = f"[[ ## sql ## ]]\n{stub.task_reply(self.stub_model, system, user)}\n\n[[ ## completed ## ]]"
        t_in, t_out = stub.tokens(system + user), stub.tokens(out)
        return Response(id=None, model=self.stub_model, message=Message.assistant([TextPart(out)]),
                        finish_reason="stop", usage=Usage(input_tokens=t_in, output_tokens=t_out,
                                                          total_tokens=t_in + t_out))

    def close(self):
        pass


class StubLM(_Capped, dspy.BaseLM):
    def __init__(self, stub_model: str, role: str, budget: Budget):
        kind = "reflect" if role == "reflect" else "task"
        super().__init__(model=f"stub/{stub_model}", engine=_StubEngine(stub_model, kind), cache=False)
        self.budget, self.role, self.price_as = budget, role, stub_model


def test_allowance(runs: int, questions: int) -> int:
    """Default cap on the test runs' calls: twice the calls they need (2 prompts x runs x test questions).

    The second half is room for a retried answer (DSPy's ChatAdapter calls again with the JSONAdapter when it
    cannot parse a reply); twice is assumed, not measured.
    """
    return 2 * (2 * runs * questions)


def priced(model: str) -> str:
    """`model` if PRICES_CAD has it. Checked before any call: a model without a price cannot count toward the cap."""
    if model not in PRICES_CAD:
        raise SystemExit(f"no price for model {model!r}: add it to PRICES_CAD in main/models.py "
                         f"(known: {', '.join(sorted(PRICES_CAD))})")
    return model


def make_lm(model: str, role: str, budget: Budget, offline: bool) -> dspy.BaseLM:
    """The LM for `model` in `role`: the stub when offline, else the course LLM gateway."""
    if offline:
        return StubLM(priced(STUB_FOR.get(model, model)), role, budget)
    return GatewayLM(priced(model), role, budget)


def gateway_env() -> tuple[str, str]:
    """The gateway's address and your token from the environment: COURSE_GATEWAY_URL, else GATEWAY_URL; KB_MCP_TOKEN,
    else COURSE_MCP_TOKEN (the older names still work)."""
    url = os.environ.get("COURSE_GATEWAY_URL") or os.environ.get("GATEWAY_URL") or ""
    token = os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN") or ""
    return url.rstrip("/"), token


def gateway_settings() -> tuple[str, str]:
    """Base URL and token from the environment: COURSE_GATEWAY_URL, else GATEWAY_URL; KB_MCP_TOKEN, else
    COURSE_MCP_TOKEN (the older names still work). Never from a file in the repo, never printed."""
    base_url, token = gateway_env()
    if not base_url or not token:
        raise SystemExit("set COURSE_GATEWAY_URL (the course gateway's base URL, ending in /v1) and KB_MCP_TOKEN")
    return base_url.rstrip("/"), token


def gateway_chat(model: str, system: str, user: str, role: str, budget: Budget) -> str:
    """One chat completion on the course gateway without DSPy (used by ext-gepa-direct and ext-eval-matrix)."""
    base_url, token = gateway_settings()
    priced(model)
    held = budget.reserve(role, model, len((system + user).encode()) + 32)   # 32: what two messages add
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    body = {"model": model, "max_tokens": max_tokens_for(role), "messages": messages}
    if is_gpt(model):
        body["temperature"] = 1.0
    req = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            reply = json.load(r)
        text = reply["choices"][0]["message"]["content"] or ""
    except (OSError, ValueError, KeyError, IndexError, TypeError) as err:
        # HTTP 401/403 (token), 429 (rate or key budget), 5xx, a network error, or a body that is not a chat
        # completion. Not retried: the caller ends the run and says why (llm-gateway shows a client that waits).
        raise budget.fail(role, err, held) from err
    u = reply.get("usage") or {}
    budget.settle(role, model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), held)
    if role == "reflect" and not text.strip():
        raise budget.fail(role, EmptyReflection(
            f"empty reply with {u.get('completion_tokens', 0)} output tokens: the reflection model used its cap of "
            f"{max_tokens_for(role)} before it wrote the instruction (raise REFLECTION_MAX_TOKENS)"))
    return text


def stub_chat(model: str, system: str, user: str, role: str, budget: Budget) -> str:
    """The same call as gateway_chat, answered by the stub."""
    name = priced(STUB_FOR.get(model, model))
    held = budget.reserve(role, name, len((system + user).encode()) + 32)
    try:
        out = stub.reflect_reply(user) if role == "reflect" else stub.task_reply(name, system, user)
    except Exception as err:  # as gateway_chat: a call that breaks is recorded, not scored
        raise budget.fail(role, err, held) from err
    budget.settle(role, name, stub.tokens(system + user), stub.tokens(out), held)
    return out
