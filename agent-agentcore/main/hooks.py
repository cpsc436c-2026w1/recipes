"""What stays in app code when an SDK runs the loop: the caps, the tool contracts, the approval and the request key.

Strands calls a hook at fixed points of its agent loop (strands.hooks, Strands Agents 1.64.0). A hook is app
code: the LLM does not see it and a prompt cannot switch it off.

  CallCap        before every model call and every tool call: count it, and cancel it past the cap
  ContractCheck  before every tool call: the arguments must match the tool's pydantic model (main/contracts.py)
  WriteGuard     before a write tool (place_hold): set the member of the request and the request key, and interrupt the agent
                 until the member approves (Strands docs, "Interrupts", BeforeToolCallEvent)
  ToolLog        after every tool call: which tools ran and which were refused, for the run report and the eval

The same hooks work for the local tools and for the tools behind the AgentCore Gateway, which names them
`<target>___<tool>`: `short_name()` drops the target.
"""
from __future__ import annotations

import time

from strands.hooks import (AfterToolCallEvent, BeforeInvocationEvent, BeforeModelCallEvent, BeforeToolCallEvent,
                           HookProvider, HookRegistry)

from main.contracts import WRITE_TOOLS, check_arguments, short_name
from main.request_key import request_key

APPROVAL_NAME = "write-approval"


class CallCap(HookProvider):
    """At most `model_calls` model calls, `tool_calls` tool calls and `seconds` seconds for one member request.

    One count for the whole request: an approval resumes the same request with a second invocation, and the
    count goes on.
    """

    def __init__(self, model_calls: int, tool_calls: int, seconds: float):
        self.max_model, self.max_tool, self.seconds = model_calls, tool_calls, seconds
        self.stopped = ""
        self.model_calls, self.tool_ids, self.deadline = 0, set(), time.monotonic() + seconds

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        over = (f"cap of {self.max_model} model calls" if self.model_calls >= self.max_model else
                f"deadline of {self.seconds:.0f} s" if time.monotonic() > self.deadline else "")
        if over:
            self.stopped = over
            event.cancel = f"Stopped by the app: {over}."     # the loop ends without this model call
        else:
            self.model_calls += 1

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        call_id = event.tool_use["toolUseId"]
        if call_id in self.tool_ids:                # the same call again, resumed after an approval
            return
        if len(self.tool_ids) >= self.max_tool:
            # This call does not run; the LLM gets the refusal as the tool result and may still reply.
            # A model that keeps asking is ended by the cap on model calls.
            self.stopped = f"cap of {self.max_tool} tool calls"
            event.cancel_tool = f"Stopped by the app: {self.stopped}. Reply now with what you have."
        else:
            self.tool_ids.add(call_id)

    @property
    def tool_calls(self) -> int:
        return len(self.tool_ids)


class ContractCheck(HookProvider):
    """A tool call whose arguments do not match the tool's contract does not run: the LLM gets the reason."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:
            return
        _, problem = check_arguments(event.tool_use["name"], dict(event.tool_use["input"] or {}))
        if problem:
            event.cancel_tool = problem


class WriteGuard(HookProvider):
    """Before a write runs: the member is the one of the request, the write has its key, and a person approves.

    `library` is anything with get_book(book_id): the local tools, or the Gateway's (main/gateway_tools.py).
    """

    def __init__(self, library, member: str, request_id: str):
        self.library, self.member, self.request_id = library, member, request_id

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:
            return
        args = event.tool_use["input"]
        if "member" in args:
            args["member"] = self.member                  # the member of the request, whatever the LLM wrote
        if short_name(event.tool_use["name"]) not in WRITE_TOOLS:
            return                                          # read tools run without a question
        book = self.library.get_book(str(args.get("book_id", "")))
        if "error" in book:
            event.cancel_tool = book["error"]
            return
        args["member"] = self.member
        args["request_id"] = request_key(self.request_id, self.member, book["book_id"])
        # The agent stops here and returns the interrupt to the caller. When the caller invokes the agent again
        # with the member's answer, this hook runs again and interrupt() returns that answer.
        answer = event.interrupt(APPROVAL_NAME, reason={
            "member": self.member, "book_id": book["book_id"], "title": book["title"], "author": book["author"]})
        if str(answer).strip().lower() not in ("y", "yes"):
            event.cancel_tool = "The member declined. No hold was placed; do not ask again."


class ToolLog(HookProvider):
    """One entry per tool call the LLM asked for: the tool, and whether it ran or why it was refused."""

    def __init__(self):
        self.calls: list[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        entry = {"tool": short_name(event.tool_use["name"]), "ran": not event.cancel_message}
        if event.cancel_message:
            entry["refused"] = str(event.cancel_message)
        self.calls.append(entry)

    @property
    def ran(self) -> list[str]:
        return [c["tool"] for c in self.calls if c["ran"]]

    @property
    def refused(self) -> list[dict]:
        return [c for c in self.calls if not c["ran"]]
