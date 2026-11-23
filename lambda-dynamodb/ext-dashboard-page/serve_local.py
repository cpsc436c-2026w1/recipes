"""Show the page on this machine: the same handler as the Lambda function, on http://localhost:8080, with your sign-in.

    uv run python ext-dashboard-page/serve_local.py                      # reads <PREFIX>-zone-counts in your account
    uv run python ext-dashboard-page/serve_local.py --now 2026-11-23T10:00:30Z   # every read at that time

Nothing is public: the server listens on 127.0.0.1 only. Each request becomes the event a function URL would send
(payload format 2.0) and goes to page.handle, so what the browser shows is what the deployed page would show.
"""
from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import page  # noqa: E402

_lock = threading.Lock()      # page.CALLS is one list per process, as it is one per Lambda environment


def event_of(method: str, target: str) -> dict:
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query))
    return {"version": "2.0", "rawPath": parts.path, "rawQueryString": parts.query,
            "queryStringParameters": query or None, "requestContext": {"http": {"method": method, "path": parts.path}}}


def make_server(port: int = 8080, now: datetime | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _any(self):
            with _lock:
                out = page.handle(event_of(self.command, self.path), now=now)
            body = out["body"].encode()
            self.send_response(out["statusCode"])
            for k, v in out.get("headers", {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _any

        def log_message(self, fmt, *args):     # one short line per request
            sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--now", help="a fixed read time (UTC), for a page of a data set as it was")
    a = p.parse_args(argv)
    server = make_server(a.port, page.utc(a.now) if a.now else None)
    print(f"the page: http://localhost:{a.port}/   (Ctrl-C ends it)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
