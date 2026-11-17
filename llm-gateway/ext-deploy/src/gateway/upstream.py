"""Calls to the upstream with httpx, and usage extraction.

The upstream is any OpenAI-compatible endpoint, named by UPSTREAM_BASE_URL.
Its key goes into the Authorization header of the upstream request only.
Nothing here logs a header or a body. With the text log on, the stream reader also keeps the reply's text for it.
"""

import json

import httpx

from . import secrets, settings

UPSTREAM_FAILED = {"error": {"message": "upstream failed", "type": "upstream_error", "code": "upstream_error"}}
UPSTREAM_BUSY = {
    "error": {"message": "the model service is busy, try again shortly", "type": "rate_limit_error",
              "code": "upstream_rate_limited"}
}
UPSTREAM_KEY_REFUSED = {
    "error": {"message": "the model service refused the gateway's own provider key. Nothing was charged. Tell the "
                         "person who issued your key.", "type": "upstream_error", "code": "upstream_key_refused"}
}
REJECTED_MESSAGE_CHARS = 300  # the provider's own message on a 4xx is passed on, cut to this length
STREAM_ENDED_EARLY = (
    b'data: {"error": {"message": "upstream stream ended early", "type": "upstream_error"}}\n\n'
    b"data: [DONE]\n\n"
)


def new_client() -> httpx.AsyncClient:
    s = settings.load()
    timeout = httpx.Timeout(connect=s.connect_timeout_s, read=s.read_timeout_s, write=30.0, pool=10.0)
    return httpx.AsyncClient(timeout=timeout)


def build_request(client: httpx.AsyncClient, path: str, body: dict) -> httpx.Request:
    base = settings.load().upstream_base_url
    if not base:
        raise RuntimeError("UPSTREAM_BASE_URL is not set")  # not sent: the caller gets 502 and the reserve back
    url = base + path
    headers = {"Authorization": f"Bearer {secrets.get_upstream_key()}", "Content-Type": "application/json"}
    return client.build_request("POST", url, content=json.dumps(body).encode(), headers=headers)


# httpx errors raised before any byte of the request reached the upstream: no connection was made, or the
# body was not sent whole, so the upstream cannot have started on it. Every other error comes after the request
# was sent (a read timeout, a dropped connection), and the provider may bill it.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
            httpx.WriteError, httpx.WriteTimeout, httpx.ProxyError, httpx.LocalProtocolError)
# 503 (unavailable) and 529 (overloaded): the provider turned the request away without generating anything.
NOT_TAKEN = (503, 529)


def was_sent(err: BaseException) -> bool:
    """True when the upstream may have received the request before `err` was raised."""
    return not isinstance(err, NOT_SENT)


def rejected_message(content: bytes, status: int) -> str:
    """The provider's own error message from a 4xx body, cut to REJECTED_MESSAGE_CHARS. The body itself is never
    passed on: only a string found at `error.message`, `error` or `message`, or a plain sentence when there is none."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        data = None
    msg = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        elif isinstance(err, str):
            msg = err
        elif isinstance(data.get("message"), str):
            msg = data["message"]
    msg = (msg or "").strip() or f"the model service rejected the request (HTTP {status})"
    return msg[:REJECTED_MESSAGE_CHARS]


def error_for_status(status: int, upstream_headers=None, content: bytes = b"") -> tuple[int, dict, dict]:
    """Map an upstream error status to the gateway's own status, body and headers.

    A 429 from the upstream keeps its Retry-After (a number of seconds or an HTTP date), so the OpenAI client's own
    retry waits as long as the upstream asked. Without one the gateway sends no Retry-After either.
    A 401 or 403 is a problem with the gateway's own provider key: 502 with a plain message. Any other 4xx is the
    provider refusing this request (a parameter the model does not take, say): 400 `upstream_rejected` with the
    provider's message, so the caller changes the request instead of resending it. A 5xx or a timeout is 502.
    """
    if status == 401:
        secrets.invalidate()  # the key may have been rotated
    if status in (401, 403):
        return 502, UPSTREAM_KEY_REFUSED, {}
    if status == 429:
        ra = (upstream_headers or {}).get("retry-after")
        return 429, UPSTREAM_BUSY, ({"Retry-After": ra} if ra else {})
    if 400 <= status < 500:
        return 400, {"error": {"message": rejected_message(content, status), "type": "invalid_request_error",
                               "code": "upstream_rejected"}}, {}
    return 502, UPSTREAM_FAILED, {}


def usage_from_body(data: dict) -> tuple[int, int] | None:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict) or "prompt_tokens" not in usage:
        return None
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class SSEMeter:
    """Reads SSE bytes on the side. Finds the usage chunk and counts content chunks.

    With `keep_text` it also puts the reply back together from its deltas, for the text log.
    """

    TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")

    def __init__(self, keep_text: bool = False) -> None:
        self._buf = b""
        self.usage: tuple[int, int] | None = None
        self.content_chunks = 0
        self.done = False
        self.keep_text = keep_text
        self._text: dict[str, list[str]] = {}
        self._tools: dict[int, dict] = {}
        self._finish: str | None = None

    def feed(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._line(line.strip())

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if payload == b"[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        u = usage_from_body(obj)
        if u is not None:
            self.usage = u
        choices = obj.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
        if delta.get("content"):
            self.content_chunks += 1
        if self.keep_text:
            self._keep(delta, choices[0].get("finish_reason"))

    def _keep(self, delta: dict, finish) -> None:
        if finish:
            self._finish = finish
        for k in self.TEXT_FIELDS:
            if isinstance(delta.get(k), str) and delta[k]:
                self._text.setdefault(k, []).append(delta[k])
        calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []
        for call in calls:
            if not isinstance(call, dict):
                continue
            # a tool call arrives in pieces that share an index: the id and name once, the arguments bit by bit
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            kept = self._tools.setdefault(call.get("index", 0), {"id": None, "name": None, "arguments": []})
            kept["id"] = call.get("id") or kept["id"]
            kept["name"] = fn.get("name") or kept["name"]
            if isinstance(fn.get("arguments"), str):
                kept["arguments"].append(fn["arguments"])

    def reply(self) -> dict | None:
        """What the stream carried, in the shape of a buffered reply's first choice. None when nothing was kept."""
        if not (self._text or self._tools or self._finish):
            return None
        reply: dict = {k: "".join(parts) for k, parts in self._text.items()}
        if self._tools:
            reply["tool_calls"] = [
                {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": "".join(t["arguments"])}}
                for _, t in sorted(self._tools.items())]
        reply["finish_reason"] = self._finish
        return reply
