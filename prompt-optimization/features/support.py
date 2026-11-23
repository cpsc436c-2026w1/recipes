"""Stand-ins and helpers the step files share.

The offline stub (main/stub.py) is the recipe's own stand-in for the two models. For a gateway run the stand-in is
`FakeGateway`: a server on 127.0.0.1 that speaks the OpenAI chat format and answers each request with what the stub
would say, so the recipe's own gateway clients (DSPy's, and the plain one in main/models.py) send real HTTP
requests that never leave this machine. It keeps every request, and it can refuse every request after the first N.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from main import stub

TOKEN = "scenario-token-not-real"       # a made-up course token
REQUEST_LIMIT = 600                      # the stand-in refuses past this, so a search that loops ends


class FakeGateway:
    def __init__(self, refuse_after: int | None = None, status: int = 429, empty_reflection: bool = False):
        self.requests: list[dict] = []
        self.refuse_after, self.status, self.empty_reflection = refuse_after, status, empty_reflection
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                    n = len(outer.requests)
                limit = outer.refuse_after if outer.refuse_after is not None else REQUEST_LIMIT
                if n > limit:
                    return self._send(outer.status, {"error": {"message": "refused by the stand-in",
                                                                  "code": "rate_limit_exceeded"}})
                system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
                user = "\n".join(m["content"] for m in body["messages"] if m["role"] == "user")
                # A reflection call: DSPy's has the field "current_instruction"; the gepa library's is one user
                # message with no system prompt. Told apart by the prompt, not by the model's name, so a run may
                # use either model in either role.
                finish = "stop"
                if "current_instruction" in user or not system:
                    text = stub.reflect_reply(user)
                    if outer.empty_reflection:   # a thinking model that spent its whole cap on reasoning
                        text, finish = "", "length"
                elif "Table trips" in system:   # the rides case of ext-eval-matrix: a plain chat call
                    import rides  # ext-eval-matrix/rides.py, on the path once matrix.py is loaded
                    text = rides.stub_reply("stub-small", system, user)
                else:
                    sql = stub.task_reply("stub-small", system, user)
                    # DSPy's ChatAdapter reads field markers; the plain client of ext-gepa-direct reads the text
                    text = f"[[ ## sql ## ]]\n{sql}\n\n[[ ## completed ## ]]" if "[[ ## " in system + user else sql
                t_in, t_out = stub.tokens(system + user), stub.tokens(text)
                self._send(200, {"id": "1", "object": "chat.completion", "created": 0, "model": body["model"],
                                 "choices": [{"index": 0, "finish_reason": finish,
                                              "message": {"role": "assistant", "content": text}}],
                                 "usage": {"prompt_tokens": t_in, "completion_tokens": t_out,
                                           "total_tokens": t_in + t_out}})

            def _send(self, status, payload):
                out = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_captured(context, fn, *args, **kwargs):
    """Call fn; keep what it printed and its exit code (a SystemExit with a message is exit code 1, as in a shell)."""
    out, err = io.StringIO(), io.StringIO()
    result, context.exit_code, context.error = None, 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                err.write(str(e.code) + "\n")
        except Exception as e:  # noqa: BLE001  a run that ends in an uncaught error: the scenario reads which
            context.exit_code, context.error = 1, e
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()
    return result


def patch(context, obj, name: str, value) -> None:
    """Replace an attribute for this scenario only."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)
