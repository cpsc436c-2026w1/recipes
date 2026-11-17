"""The routed app: label the request, run the path for that label, log what went wrong.

`handle(text, models, log)` is the one function your own app calls (README, "Adapt it").
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from main.paths import PATHS, PathResult
from main.router import ROUTER_MAX_TOKENS, ROUTER_SYSTEM, parse_label


# What a failed model call raises: the gateway client's own error for a refusal (main/model.py), and httpx's for
# a 5xx status, a time limit that passed or a connection that failed.
MODEL_CALL_FAILED = (RuntimeError, httpx.HTTPError)


@dataclass
class Models:
    """Which client, and which model plays which part. App code reads these; the model never chooses."""
    client: object           # GatewayModel or StubModel
    small: str
    large: str

    def chat(self, model, system, user, max_tokens):
        return self.client.chat(model, system, user, max_tokens)


class MisrouteLog:
    """One JSON line per request whose route did not work out: an unknown label, or a path that gave up.

    Read it every week: each line is a request your router prompt or your paths do not cover yet. Lines carry the
    request text, so keep the file where your users' data may live (README, "Decisions": misroute log).
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.lines: list[dict] = []

    def write(self, **entry) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self.lines.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")


class RouteLog(MisrouteLog):
    """One JSON line per request, also for the ones that went well: time, request id, label, path, large-model
    calls, cost, duration, and whether the request was misrouted or ended in error.

    The misroute log alone shows only what failed; how often each path runs and what each costs needs every
    request. A line holds no request text and no reply, so this file can be kept and shared more freely than the
    misroute log (README, "Decisions": route log).
    """


def handle(text: str, models: Models, log: MisrouteLog, request_id: str | None = None,
           routes: MisrouteLog | None = None) -> dict:
    request_id = request_id or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    calls: list = []                       # (part, Reply) of every model call that was answered
    label, path, reply, error, misrouted = None, "error", "", "", False

    pending = None                         # the misroute line of this request, written once its outcome is known
    try:
        # 1. the small model labels the request
        r = models.chat(models.small, ROUTER_SYSTEM, text, ROUTER_MAX_TOKENS)
        calls.append(("router", r))
        route = parse_label(r.text)
        label = route.label
        if not route.known:
            pending = dict(request_id=request_id, request=text, label=None, router_reply=route.raw[:80],
                           reason="router reply is not a label", sent_to="hard")

        # 2. app code runs the fixed path for the label
        result: PathResult = PATHS[label](text, models)
        path = label
        if result.misroute:
            # the path could not do its job: log it and give the request to the hard path rather than guess
            pending = dict(request_id=request_id, request=text, label=label, reason=result.misroute, sent_to="hard")
            calls += [(label, c) for c in result.calls]      # what the path spent before it gave up still counts
            result = PATHS["hard"](text, models)
            path = "hard"
        calls += [(path, c) for c in result.calls]
        reply = result.reply
    except MODEL_CALL_FAILED as e:
        # A refusal by the gateway, a 5xx, no answer in time or a reply that is not a chat completion: the request
        # ends here, with the reason, and the app goes on. Nothing is sent again: a retry on a spent budget only
        # fails again, and a call that timed out may still have been charged (README, "Decisions").
        path, error = "error", f"{type(e).__name__}: {e}"[:300]
    if pending is not None:
        # Written after the hard path has answered or failed, so the line says how the request ended: a misroute
        # whose hard call failed has "error": true and was not answered.
        log.write(**pending, **({"error": True} if error else {}))
        misrouted = True

    cost = round(sum(c.cost_cad for _, c in calls), 8)
    large = sum(1 for _, c in calls if c.model == models.large)
    ms = round((time.perf_counter() - started) * 1000)
    if routes is not None:
        routes.write(request_id=request_id, label=label, path=path, misrouted=misrouted, error=bool(error),
                     large_model_calls=large, cost_cad=cost, ms=ms)
    return {
        "request_id": request_id,
        "request": text,
        "label": label,
        "path": path,
        "reply": reply,
        "error": error,
        "calls": [{"part": part, "model": c.model, "tokens_in": c.tokens_in, "tokens_out": c.tokens_out,
                   "cost_cad": round(c.cost_cad, 8)} for part, c in calls],
        "large_model_calls": large,
        "cost_cad": cost,
        "ms": ms,
    }
