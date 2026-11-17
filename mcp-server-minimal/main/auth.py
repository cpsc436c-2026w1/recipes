"""Bearer-token check in front of the MCP endpoint (README, "Decisions": token auth).

An ASGI wrapper: every HTTP request must carry `Authorization: Bearer <token>`; anything else gets 401 before the
MCP code sees it. The check compares SHA-256 hashes in constant time, so the time an answer takes does not tell a
caller how much of a guess was right. The token is never logged.

Why not the SDK's own `token_verifier`: in the MCP Python SDK it comes with `AuthSettings`, which expect an OAuth
authorization server that issues the tokens. A team with one shared token does not run one; this wrapper is the
smallest check that does the job. Move to the SDK's OAuth route when your users log in (README, "Adapt it").
"""

from __future__ import annotations

import hashlib
import hmac


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class BearerTokenAuth:
    def __init__(self, app, token: str):
        if len(token) < 16:
            raise SystemExit("MCP_SERVER_TOKEN must be set and at least 16 characters (README, 'Run it')")
        self.app = app
        self._hash = token_hash(token)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            scheme, _, presented = header.partition(" ")
            ok = scheme.lower() == "bearer" and hmac.compare_digest(token_hash(presented), self._hash)
            if not ok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="mcp"')]})
                await send({"type": "http.response.body", "body": b'{"error": "missing or wrong bearer token"}'})
                return
        await self.app(scope, receive, send)
