"""One Runtime session = one member conversation: the requests the Runtime's entry point answers.

    {"mode": "request", "prompt": "Which is shorter, Dune or Emma? Place a hold on the shorter one.", "member": "sam"}
    {"mode": "approve", "answer": "y"}       # answers the approval the "request" call returned, in the same session

An agent that waits for an approval is kept in this process: AgentCore routes every call of one session to the same
microVM until the session ends, and keeps its memory between the calls (AgentCore docs, "Use isolated sessions for
agents"). When the microVM is gone (idle timeout, maximum lifetime), the waiting agent is gone with it and an
approval gets status "error".
src: receipt: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html read 2026-11-16
"""
from __future__ import annotations

from main import agent as bounded
from main import tracing
from main.config import Settings


class Sessions:
    def __init__(self, s: Settings, model, library, tools: list | None = None, traced: bool = False):
        self.s, self.model, self.library, self.tools, self.traced = s, model, library, tools, traced
        self.waiting: dict[str, tuple] = {}      # session id -> (agent, run, approval ids)

    def handle(self, payload: dict, session_id: str) -> dict:
        mode = str(payload.get("mode", "request"))
        if mode == "request":
            if not payload.get("prompt") or not payload.get("member"):
                return {"status": "error", "error": "a request needs a prompt and a member"}
            agent, run = bounded.build(self.s, self.model, self.library, str(payload["member"]), tools=self.tools)
            with tracing.request_span(self.traced, run.request_id, run.member):
                out = bounded.start(agent, run, str(payload["prompt"]))
        elif mode == "approve":
            if session_id not in self.waiting:
                return {"status": "error", "error": "no approval is waiting in this session"}
            agent, run, pending = self.waiting.pop(session_id)
            with tracing.request_span(self.traced, run.request_id, run.member):
                out = bounded.resume(agent, run, {i: str(payload.get("answer", "n")) for i in pending})
        else:
            return {"status": "error", "error": f"unknown mode {mode}"}
        if out["status"] == "needs_approval":
            self.waiting[session_id] = (agent, run, [a["id"] for a in out["approvals"]])
        else:
            out["holds"] = self.library.get_holds(run.member)
        return {**out, "mode": mode, "runtime_session": session_id}
