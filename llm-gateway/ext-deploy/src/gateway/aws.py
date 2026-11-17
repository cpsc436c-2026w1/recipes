"""Lazy boto3 handles, created once per container.

Tests call `reset()` after moto starts so new handles see the mock.
"""

import boto3

from . import settings

_cache: dict[str, object] = {}


def _get(name: str, factory):
    if name not in _cache:
        _cache[name] = factory()
    return _cache[name]


def keys_table():
    s = settings.load()
    return _get("keys", lambda: boto3.resource("dynamodb", region_name=s.region).Table(s.table_keys))


def usage_table():
    s = settings.load()
    return _get("usage", lambda: boto3.resource("dynamodb", region_name=s.region).Table(s.table_usage))


def texts_table():
    s = settings.load()
    return _get("texts", lambda: boto3.resource("dynamodb", region_name=s.region).Table(s.table_texts))


def sns():
    s = settings.load()
    return _get("sns", lambda: boto3.client("sns", region_name=s.region))


def secretsmanager():
    s = settings.load()
    return _get("sm", lambda: boto3.client("secretsmanager", region_name=s.region))


def reset() -> None:
    _cache.clear()
