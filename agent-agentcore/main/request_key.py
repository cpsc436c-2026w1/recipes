"""The request key of one write: the same request, member and book always give the same key.

The store refuses a second row with a key it already holds (main/store.py), so a write that is sent twice, by a
retry or by a model that asks again, changes the data once (Agentic AI Lens AGENTREL06-BP04: a deterministic
idempotency key and a conditional write).
"""
from __future__ import annotations

import hashlib

KEY_CHARS = 16    # 64 bits of the hash: enough to tell the writes of one member apart. src: assumed


def request_key(request_id: str, member: str, book_id: str) -> str:
    if not (request_id and member and book_id):
        raise ValueError("a request key needs the request id, the member and the book id")
    return hashlib.sha256(f"{request_id}:{member}:{book_id}".encode()).hexdigest()[:KEY_CHARS]
