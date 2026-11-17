"""Offline fixtures: moto stands in for DynamoDB; fake credentials; a clock the tests can move."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from config import Settings
from tables import ensure_tables


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def s():
    return Settings()


@pytest.fixture
def ddb(s):
    return boto3.resource("dynamodb", region_name=s.region)


@pytest.fixture
def tables(s, ddb):
    ensure_tables(s, ["conversations", "history", "cache", "traces"], ddb.meta.client)
    return ddb
