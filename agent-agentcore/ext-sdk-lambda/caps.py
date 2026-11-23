"""The Strands hooks of this extension: the limits of one question live in app code (README, Decisions).

Adapted from agent-agentcore's main/hooks.py (CallCap, ContractCheck), which this extension leaves as it is. What
changes here:
  - CallCap checks the deadline before a tool call too, and does not count the structured-output tool `TripAnswer`
    (Strands 1.64.0 runs structured output as a tool named after the model's class), so a question of 3 queries
    and its answer is not stopped on its output;
  - the tool cap ends the question (status `stopped`) instead of letting the model reply once more;
  - OutputRetry ends the question at the second TripAnswer call that does not validate;
  - SqlContract checks the arguments of run_sql against RunSqlArgs.

The 25 s deadline is also enforced on whatever is in flight, by asyncio.timeout_at around the whole agent
(agent_strands.py): a check before a call does not stop a call that started at 24.9 s.
"""
from __future__ import annotations

from pydantic import ValidationError
from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from toollog import Deadline
from trip_contracts import OUTPUT_NAME, TOOL_NAME, RunSqlArgs, TripAnswer


class OutputRetry(HookProvider):
    """One retry for an output that does not validate: the first bad TripAnswer goes back to the model as an
    error (Strands does that), the second ends the question as `output_format`."""

    def __init__(self, retries: int = 1):
        self.retries, self.failures, self.failed = retries, 0, False

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != OUTPUT_NAME:
            return
        try:
            TripAnswer.model_validate(dict(event.tool_use["input"] or {}))
        except ValidationError:
            self.failures += 1
            if self.failures > self.retries:
                self.failed = True
                event.cancel_tool = "Stopped by the app: the output did not validate after one retry."

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if self.failed:
            event.cancel = "Stopped by the app: output_format."


class CallCap(HookProvider):
    """At most `model_calls` model calls and `tool_calls` run_sql calls, and nothing started after the deadline."""

    def __init__(self, model_calls: int, tool_calls: int, deadline: Deadline):
        self.max_model, self.max_tool, self.deadline = model_calls, tool_calls, deadline
        self.stopped = ""
        self.model_calls, self.tool_ids = 0, []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def _over(self, cap_reached: str) -> str:
        if self.stopped:
            return self.stopped
        if cap_reached:
            return cap_reached
        if self.deadline.passed():
            return f"deadline of {self.deadline.seconds:g} s"
        return ""

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if event.cancel:                       # OutputRetry ended the question first
            return
        over = self._over(f"cap of {self.max_model} model calls" if self.model_calls >= self.max_model else "")
        if over:
            self.stopped = over
            event.cancel = f"Stopped by the app: {over}."
        else:
            self.model_calls += 1

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] == OUTPUT_NAME:  # the final output, not a query: not counted, not capped
            return
        call_id = event.tool_use["toolUseId"]
        if call_id in self.tool_ids:
            return
        over = self._over(f"cap of {self.max_tool} run_sql calls" if len(self.tool_ids) >= self.max_tool else "")
        if over:
            self.stopped = over
            event.cancel_tool = f"Stopped by the app: {over}."
        else:
            self.tool_ids.append(call_id)

    @property
    def tool_calls(self) -> int:
        return len(self.tool_ids)


class SqlContract(HookProvider):
    """A run_sql call whose arguments do not match RunSqlArgs does not run: the model gets the reason."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool or event.tool_use["name"] == OUTPUT_NAME:
            return
        if event.tool_use["name"] != TOOL_NAME:
            event.cancel_tool = f"unknown tool {event.tool_use['name']}; the one tool is {TOOL_NAME}"
            return
        try:
            RunSqlArgs.model_validate(dict(event.tool_use["input"] or {}))
        except ValidationError as err:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                                 for e in err.errors())
            event.cancel_tool = f"wrong arguments for {TOOL_NAME}: {problems}"
