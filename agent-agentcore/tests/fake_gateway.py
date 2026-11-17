"""A local stand-in for the course LLM gateway: an OpenAI-compatible /v1/chat/completions that streams scripted
replies and keeps every request it got. It listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tool_call(name: str, **arguments) -> dict:
    return {"tool": name, "arguments": arguments}


class FakeGateway:
    def __init__(self, script: list):
        self.script, self.requests = list(script), []
        self.status = 200                 # set to 429 or 500: every request is then refused with that status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
                n = len(outer.requests)
                if outer.status != 200:
                    refusal = json.dumps({"error": {"message": "refused by the stand-in", "type": "test"}}).encode()
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(refusal)))
                    self.end_headers()
                    self.wfile.write(refusal)
                    return
                step = outer.script[min(n, len(outer.script)) - 1]
                if isinstance(step, dict):
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call-{n}", "type": "function",
                             "function": {"name": step["tool"], "arguments": json.dumps(step["arguments"])}}]}
                    finish = "tool_calls"
                else:
                    delta, finish = {"role": "assistant", "content": step}, "stop"
                base = {"id": f"chatcmpl-{n}", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
                chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                          {**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                                            "total_tokens": 110}}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
