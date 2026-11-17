"""Exact answer cache, looked up before the model is called (ext-answer-cache/README.md).

Key = SHA-256 of (normalized question, data version, scope, answer version).
- normalized question: lower case, single spaces, no trailing punctuation. "How many orders?" and
  "how many  orders" share an entry; "how many orders were there" does not (exact cache, not semantic).
- data version: a hash of the Parquet files (main/data.py). New data, new keys: an old answer is never served
  for new data, and no one has to remember to flush.
- scope: "shared" for questions whose answer is the same for everyone; "user:<hash>" when the answer depends on
  who asks ("my", "I", "me"). One user's answer is never served to another.
- answer version: the model's name and a hash of the system prompt and the tool definition (main/prompts.py).
  A new model or new rules, new keys: an answer the old ones wrote is not served.
Questions about "today", "now" or "this week" change meaning with the clock, so they are not cached at all.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal

from main.prompts import prompt_version

CACHE_TTL_S = 24 * 3600  # ours: the data version already ends an entry when data changes; this bounds the rest

_PERSONAL = re.compile(r"\b(i|me|my|mine|myself|i'm|i've)\b", re.I)
_CLOCK = re.compile(r"\b(today|now|yesterday|tonight|this (week|month|year)|last (week|month|year)|current)\b", re.I)


def normalize(question: str) -> str:
    q = re.sub(r"\s+", " ", question.strip().lower())
    return q.rstrip("?.! ")


def scope_of(question: str, user_id: str, per_user_rows: bool = False) -> str | None:
    """None: do not cache. Otherwise the scope that goes into the key.

    per_user_rows: the answers come from an engine that holds only the asking user's rows (ext-guardrails-redteam).
    Then every answer is that user's own, whatever the question's words, and the scope is never "shared".
    """
    if _CLOCK.search(question):
        return None
    if per_user_rows or _PERSONAL.search(question):
        return "user:" + hashlib.sha256(user_id.encode()).hexdigest()[:16]
    return "shared"


def answer_version(model_name: str) -> str:
    """What wrote the answer: the model and the version of the system prompt and the tool definition."""
    return f"{model_name}:{prompt_version()}"


def cache_key(question: str, data_version: str, scope: str, answer_version: str) -> str:
    raw = json.dumps([normalize(question), data_version, scope, answer_version])
    return hashlib.sha256(raw.encode()).hexdigest()


class LocalCache:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        item = self.items.get(key)
        return item if item and item["expires_at"] > time.time() else None

    def put(self, key: str, value: dict) -> None:
        self.items[key] = {**value, "expires_at": int(time.time()) + CACHE_TTL_S}


class DynamoCache:
    """Table {PREFIX}-chat-cache, partition key cache_key, time to live on expires_at (main/infra.py)."""

    def __init__(self, table_name: str, region: str, resource=None):
        import boto3

        self.table = (resource or boto3.resource("dynamodb", region_name=region)).Table(table_name)

    def get(self, key: str) -> dict | None:
        try:
            item = self.table.get_item(Key={"cache_key": key}).get("Item")
        except Exception as e:  # a cache that fails is a miss, never an error for the user
            print(f"cache get failed: {e}")
            return None
        # DynamoDB deletes expired items some time after expiry, so the reader checks the time itself.
        if not item or int(item["expires_at"]) <= time.time():
            return None
        return json.loads(item["value"])

    def put(self, key: str, value: dict) -> None:
        try:
            self.table.put_item(Item={"cache_key": key, "value": json.dumps(value),
                                      "expires_at": Decimal(int(time.time()) + CACHE_TTL_S)})
        except Exception as e:
            print(f"cache put failed: {e}")


def cached_answer(question: str, *, user_id: str, cache, data_version: str, answer_version: str, ask,
                  per_user_rows: bool = False):
    """Look up first; on a miss call ask() (main.chat.answer with its arguments bound) and store an answered result.

    answer_version has no default on purpose: a caller that leaves it out would serve answers of an old model or an
    old prompt. Returns (answer_dict, hit). Only "answered" results are stored: a refusal or an error is not worth
    repeating.
    """
    # Decision (who asks): with per_user_rows no answer is stored or served under the shared scope.
    scope = scope_of(question, user_id, per_user_rows)
    key = cache_key(question, data_version, scope, answer_version) if scope else None
    if key:
        hit = cache.get(key)
        if hit is not None:
            return {**hit, "status": "cache_hit", "cost_cad": 0.0, "tokens_in": 0, "tokens_out": 0}, True
    ans = ask(question)
    d = ans.to_dict() if hasattr(ans, "to_dict") else dict(ans)
    if key and d["status"] == "answered":
        keep = {k: d[k] for k in ("question", "reply", "sql", "columns", "rows", "model", "data_version")}
        cache.put(key, keep)
    return d, False
