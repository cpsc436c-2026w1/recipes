"""Exact answer cache, looked up before retrieval (ext-answer-cache/README.md).

Table {PREFIX}-answer-cache, partition key cache_key, time to live on expires_at (main/tables.py).

cache_key = SHA-256 of (normalized question, index version, scope)
- normalized question: lower case, single spaces, trailing ?.! removed. Exact, not semantic.
- index version: the version of the search index the answer was built from. Re-index, bump the version, and
  every old entry stops matching without a flush.
- scope: "shared", or "user:<hash of the user id>" when the answer depends on who asks.

An answer is stored as shared only when the question has no personal words AND none of the retrieved documents
belongs to a user. Everything else is stored under the asking user's scope, so one user's answer never reaches
another user.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal

from config import CACHE_TTL_HOURS, PERSONAL_CACHE_TTL_HOURS

_PERSONAL = re.compile(r"\b(i|me|my|mine|myself|i'm|i've|our|we)\b", re.I)


def normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower()).rstrip("?.! ")


def user_scope(user_id: str) -> str:
    return "user:" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


def cache_key(question: str, index_version: str, scope: str) -> str:
    # README "Decisions", row "Version in the key": the index version is part of the key, never a field checked later.
    return hashlib.sha256(json.dumps([normalize(question), index_version, scope]).encode()).hexdigest()


class AnswerCache:
    def __init__(self, table_name: str, resource, clock=time.time, ttl_hours: int = CACHE_TTL_HOURS,
                 personal_ttl_hours: int = PERSONAL_CACHE_TTL_HOURS):
        self.table = resource.Table(table_name)
        self.clock = clock
        self.ttl_s = ttl_hours * 3600
        self.personal_ttl_s = personal_ttl_hours * 3600
        # Every lookup is one hit or one miss, counted for the life of this object. In Lambda, a cache made at module
        # level lives as long as the execution environment and keeps counting across requests: emit the change of
        # each request (1 hit or 1 miss), not the totals (README, "Decisions", row "Hit rate").
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        item = self.table.get_item(Key={"cache_key": key}).get("Item")
        # DynamoDB deletes expired items some time after expiry, so the reader checks the time itself.
        if not item or int(item["expires_at"]) <= self.clock():
            return None
        return json.loads(item["value"])

    def put(self, key: str, value: dict, personal: bool = False) -> None:
        # README "Decisions", row "Expiry": an answer for one user is about their data, which changes sooner than
        # a policy shared by everyone (Agentic AI Lens AGENTCOST02-BP03: one TTL for every kind of entry).
        ttl_s = self.personal_ttl_s if personal else self.ttl_s
        self.table.put_item(Item={"cache_key": key, "value": json.dumps(value),
                                  "expires_at": Decimal(int(self.clock() + ttl_s))})


def answer_with_cache(question: str, *, user_id: str, index_version: str, cache: AnswerCache, retrieve, generate):
    """Return (answer, hit). `retrieve(question, user_id)` returns documents as dicts with "id", "text" and
    "owner" ("" for a public document); `generate(question, docs)` returns the reply text (your model call through
    the course LLM gateway; a stub in this recipe).
    """
    mine = cache_key(question, index_version, user_scope(user_id))
    shared = cache_key(question, index_version, "shared")
    personal = bool(_PERSONAL.search(question))
    # README "Decisions", row "Where the lookup sits": before retrieval, so a hit skips the search and the model call.
    for key in ([mine] if personal else [mine, shared]):
        hit = cache.get(key)
        if hit is not None:
            cache.hits += 1
            return hit, True
    cache.misses += 1
    docs = retrieve(question, user_id)
    reply = generate(question, docs)
    private = personal or any(d.get("owner") for d in docs)
    answer = {"question": question, "reply": reply, "sources": [d["id"] for d in docs],
              "index_version": index_version, "scope": "user" if private else "shared"}
    # README "Decisions", rows "Who asks" and "What is stored as shared": an answer built from one user's question
    # or documents is never stored where another user looks.
    cache.put(mine if private else shared, answer, personal=private)
    return answer, False
