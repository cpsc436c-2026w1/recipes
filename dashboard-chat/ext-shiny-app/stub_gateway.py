"""A stand-in for the course LLM gateway, for offline runs of the app: no key, no network beyond this machine.

It listens on 127.0.0.1 and answers POST /v1/chat/completions in the OpenAI chat format, as the gateway does, so
the app talks to it with the same client and the same code path. It does not call a model. Its replies are fixed:
- a question in stub_replies.json gets a tool call: {"filter": SQL, "title": ...} asks querychat to filter the
  dashboard's table (querychat_update_dashboard), {"query": SQL} asks for a result in the chat (run_sql, the
  recipe's own query tool, source.py); a "query" that is a list of SQL strings is one tool call per model call;
- after a tool result it replies with one sentence;
- any other question gets "I can only answer questions about the shop's sales."
Token counts are estimates (characters / 4), as in main/model.py's stub.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chatformat import completion

REPLIES = Path(__file__).with_name("stub_replies.json")
DECLINE = "I can only answer questions about the shop's sales."
RUNAWAY_CALLS = 10  # an "always" question answers this many calls, then fails the request: a test must not hang


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower()).rstrip("?.! ")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def _always(messages: list, script: dict) -> bool:
    question = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    return bool((script.get(_norm(question)) or {}).get("always"))


def stub_reply(body: dict, script: dict, table: str) -> dict:
    """The assistant message for one request: {"content": ...} or {"tool_calls": [...]}."""
    messages = body.get("messages", [])
    last = messages[-1] if messages else {}
    if last.get("role") == "tool" and not _always(messages, script):
        failed = "error" in _text(last.get("content")).lower()[:200]
        return {"content": "I could not run a query for that question." if failed else
                "Here is what the shop's data shows for that question."}
    question = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    step = script.get(_norm(question))
    if step is None:
        return {"content": DECLINE}
    if step.get("always"):  # a model that asks for the tool on every call, whatever the tool result was
        calls = sum(m.get("role") == "assistant" for m in messages[max(
            (i for i, m in enumerate(messages) if m.get("role") == "user"), default=0):])
        if calls >= RUNAWAY_CALLS:  # nothing stopped the loop: fail the caller, do not go on for ever
            raise RuntimeError(f"the stand-in was asked {calls + 1} times for one question: no step limit is in force")
        step, last = {"query": step["always"]}, {}
    if "filter" in step:
        name, args = "querychat_update_dashboard", {"table": table, "query": step["filter"], "title": step["title"]}
    else:
        name, args = "run_sql", {"sql": step["query"]}
    offered = {t["function"]["name"]: t["function"].get("parameters", {}).get("properties", {})
               for t in body.get("tools") or []}
    args = {k: v for k, v in args.items() if k in offered.get(name, args)}  # only arguments the tool has
    return {"content": None, "tool_calls": [{"id": f"call-{len(messages)}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


class Handler(BaseHTTPRequestHandler):
    script: dict = {}
    table = "orders"
    requests: list = []  # every request body, for the tests

    def log_message(self, *args):  # no access log on the terminal
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        type(self).requests.append({"path": self.path, "authorization": self.headers.get("Authorization", ""),
                                    "body": body})
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "the stub answers /chat/completions only", "code": "404"}})
        try:
            message = stub_reply(body, self.script, self.table)
        except RuntimeError as e:
            return self._send(500, {"error": {"message": str(e), "code": "stand_in_runaway"}})
        model = body.get("model", "stub")
        usage = {"prompt_tokens": len(json.dumps(body.get("messages", []))) // 4,
                 "completion_tokens": len(json.dumps(message)) // 4}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        content_type, data = completion(message, model, bool(body.get("stream")), usage)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(table: str = "orders", script: dict | None = None) -> tuple[str, ThreadingHTTPServer]:
    """Start the stub on a free port of 127.0.0.1. Returns its base URL (ending in /v1) and the server."""
    raw = script if script is not None else json.loads(REPLIES.read_text())
    handler = type("StubHandler", (Handler,), {"script": {_norm(q): v for q, v in raw.items()}, "table": table,
                                               "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", server
