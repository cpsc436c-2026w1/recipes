"""Cached read of the upstream key from Secrets Manager.

One GetSecretValue per container, refreshed after SECRET_TTL_S (15 minutes) or
after the upstream answers 401. The value is never logged and never returned to a caller.
"""

import threading
import time

from . import aws, settings

_lock = threading.Lock()
_value: str | None = None
_fetched_at = 0.0
reads = 0  # number of GetSecretValue calls in this container, for tests


def get_upstream_key() -> str:
    # Decision (provider key, ../README.md): the one provider key lives in Secrets Manager, read by the function's role;
    # it never reaches a caller, a log line or the repo.
    global _value, _fetched_at, reads
    ttl = settings.load().secret_ttl_s
    with _lock:
        if _value is None or time.monotonic() - _fetched_at > ttl:
            resp = aws.secretsmanager().get_secret_value(SecretId=settings.load().secret_arn)
            _value = resp["SecretString"]
            _fetched_at = time.monotonic()
            reads += 1
        return _value


def invalidate() -> None:
    """Forget the cached key so the next request reads it again."""
    global _value
    with _lock:
        _value = None


def reset_for_tests() -> None:
    global reads
    invalidate()
    reads = 0
