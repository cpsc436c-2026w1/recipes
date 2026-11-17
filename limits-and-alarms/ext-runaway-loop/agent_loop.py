"""An agent-style loop with its own limits in app code, and one EMF line per task saying why it stopped.

Limits in the loop (ext-runaway-loop/README.md, "Decisions"):
- a step cap: at most `max_steps` model calls per task
- a deadline: no new model call after `deadline_s` seconds; in Lambda, never later than the function's own timeout
- a context cap: only the newest `keep_messages` tool results go back to the model
- a cost cap: no model call that could take the task's spend past `max_cost_cad`, counted at the most the call can
  cost (its context and `max_tokens` of output at the model's price)
- no retry on a gateway refusal: a 429 for budget or rate ends the task

Limits outside it, which hold when the loop's own limits have a bug: the gateway's per-key budget and rate, the
Lambda timeout and reserved concurrency, and the daily-cost alarm (main/).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config import cost_cad
from emf import emit, request_line
from fake_gateway import GatewayRefused

SYSTEM_PROMPT_TOKENS = 500   # assumed size of the system prompt and the task
TOOL_RESULT_TOKENS = 50      # assumed size of one tool result


@dataclass
class Limits:
    max_steps: int | None = 8        # ours (assumed): a task that needs more is a task to redesign
    deadline_s: float | None = 20.0  # ours (assumed): under the 30 s Lambda timeout, with time to answer
    keep_messages: int = 10          # ours (assumed)
    max_tokens: int = 400            # output cap per model call
    max_cost_cad: float | None = 0.01  # ours (assumed): about 200 calls of the small model with a short context
    model: str = "qwen3.6-35b-a3b"   # the model whose price the cost cap uses


@dataclass
class Result:
    stop_reason: str   # done, step_cap, deadline, cost_cap, budget_exhausted, rate_limited
    steps: int
    cost_cad: float
    seconds: float


def deadline_for(context, cap_s: float, margin_s: float = 2.0) -> float:
    """In Lambda: the code's deadline is the smaller of its own cap and the time Lambda still allows, minus a
    margin to write the reply and the metrics (README, "Decisions": deadline)."""
    remaining = context.get_remaining_time_in_millis() / 1000 if context else float("inf")
    return max(0.0, min(cap_s, remaining - margin_s))


def run_agent(task: str, *, call_model, tools: dict, limits: Limits, clock=time.monotonic,
              namespace: str = "recipe/app", service: str = "recipe-app", out=None) -> Result:
    # README, "Decisions", row "Cost cap": the cap prices calls at limits.model, so the gateway client must say which
    # model it asks for (a .model attribute, as the stand-in has), and it must be the same one.
    asked = getattr(call_model, "model", None)
    if limits.max_cost_cad is not None and asked != limits.model:
        raise ValueError(f"the cost cap prices {limits.model}, and the gateway client asks for "
                         f"{asked if asked is not None else 'a model it does not name (no .model)'}: give call_model "
                         f"a .model and set Limits(model=...) to it")
    start = clock()
    history: list[str] = []
    steps, spent, reason, tin, tout = 0, 0.0, "done", 0, 0
    while True:
        # README, "Decisions": both checks run before each model call, never after it.
        if limits.max_steps is not None and steps >= limits.max_steps:
            reason = "step_cap"
            break
        if limits.deadline_s is not None and clock() - start >= limits.deadline_s:
            reason = "deadline"
            break
        context = SYSTEM_PROMPT_TOKENS + TOOL_RESULT_TOKENS * len(history[-limits.keep_messages:])
        # README, "Decisions": the cost cap. Checked at the most this call can cost, before it is sent.
        if limits.max_cost_cad is not None and spent + cost_cad(limits.model, context, limits.max_tokens) > limits.max_cost_cad:
            reason = "cost_cap"
            break
        try:
            r = call_model(context, limits.max_tokens)
        except GatewayRefused as e:
            reason = e.code  # README, "Decisions": no retry; the refusal says why
            break
        steps += 1
        spent += r.cost_cad
        tin, tout = tin + r.tokens_in, tout + r.tokens_out
        if r.tool_call is None:
            break
        history.append(str(tools[r.tool_call["name"]](**r.tool_call["arguments"])))

    seconds = clock() - start
    doc = request_line(namespace, service, latency_ms=seconds * 1000, error=reason != "done", cost_cad=spent,
                       tokens_in=tin, tokens_out=tout, extra={"StopReason": reason, "AgentSteps": steps, "task": task[:80]})
    # A second directive with one metric, per stop reason: at most six extra custom metrics (one per reason),
    # each billed. Putting StopReason on the first directive would multiply all six request metrics by six.
    doc["_aws"]["CloudWatchMetrics"].append({"Namespace": namespace, "Dimensions": [["Service", "StopReason"]],
                                             "Metrics": [{"Name": "AgentSteps", "Unit": "Count"}]})
    emit(doc, out=out)
    return Result(reason, steps, spent, seconds)
