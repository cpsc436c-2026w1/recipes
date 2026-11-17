"""Bearer key parsing, hashing, lookup and state checks.

A key looks like `gw-ABCDEFGH-<43 url-safe characters>`. The part before the
second dash is the key id (`settings.KEY_PREFIX` sets its prefix). Only the SHA-256 hex of the whole key is stored.

A key that another system made can be added by its SHA-256 hex with `gw-admin import`. Such a key is 43 url-safe
characters with no prefix, which is what Python's `secrets.token_urlsafe(32)` returns.
"""

import hashlib
import hmac
import re
import time

from . import aws
from .settings import KEY_PREFIX

KEY_RE = re.compile(rf"^({re.escape(KEY_PREFIX)}-[A-Z2-7]{{8}})-[A-Za-z0-9_-]{{43}}$")
# Keys minted elsewhere are URL-safe, 32 to 64 characters (secrets.token_urlsafe(24) to (48)).
IMPORTED_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class GatewayError(Exception):
    """An error the client sees, in the OpenAI error shape."""

    def __init__(self, status: int, message: str, type_: str, code: str | None = None, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.headers = headers or {}
        self.key_id: str | None = None  # set once the key is known, for the refusal's log line


def hash_key(key: str) -> str:
    # Decision (keys, ../README.md): only the SHA-256 of a caller's key is stored, so a copy of the table opens nothing.
    return hashlib.sha256(key.encode()).hexdigest()


def parse_bearer(header: str | None) -> str:
    """Return the key from an Authorization header, or raise 401. No AWS call."""
    if not header or not header.startswith("Bearer "):
        raise GatewayError(401, "missing or malformed API key", "invalid_request_error", "invalid_api_key")
    key = header[len("Bearer "):].strip()
    if not (KEY_RE.match(key) or IMPORTED_KEY_RE.match(key)):
        raise GatewayError(401, "missing or malformed API key", "invalid_request_error", "invalid_api_key")
    return key


def lookup(key: str) -> dict:
    """Read the key item with a strongly consistent read. Unknown key: 401."""
    h = hash_key(key)
    item = aws.keys_table().get_item(Key={"key_hash": h}, ConsistentRead=True).get("Item")
    if not item or not hmac.compare_digest(str(item.get("key_hash", "")), h):
        raise GatewayError(401, "unknown API key", "invalid_request_error", "invalid_api_key")
    return item


def check_state(item: dict, now: int | None = None) -> None:
    """Revoked or expired: 403."""
    now = int(time.time()) if now is None else now
    if item.get("state") != "active":
        raise GatewayError(403, "this key has been revoked", "permission_error", "key_revoked")
    if int(item.get("expires_at", 0)) <= now:
        raise GatewayError(403, "this key has expired", "permission_error", "key_expired")


def check_model(item: dict, model: str) -> None:
    allowed = set(item.get("allowed_models", set()))
    if model not in allowed:
        raise GatewayError(
            403,
            f"model {model!r} is not allowed for this key. Allowed: {', '.join(sorted(allowed))}",
            "permission_error",
            "model_not_allowed",
        )
