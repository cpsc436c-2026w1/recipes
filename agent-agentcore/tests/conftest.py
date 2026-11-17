"""Offline fixtures: the settings of .env.example, made-up AWS credentials, moto for the AWS services it has and
a stand-in for AgentCore (tests/fakes.py). No network, no key, no account."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.offline_env import example_env, example_lines, no_wait  # noqa: E402,F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    saved = dict(os.environ)
    example_env()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def s():
    from main.config import load_settings

    return load_settings()


@pytest.fixture
def library():
    from main.store import MemoryStore
    from main.tools import LibraryTools

    return LibraryTools(MemoryStore())


@pytest.fixture
def aws():
    """moto for DynamoDB, IAM, Lambda, S3, Parameter Store, CloudWatch Logs and STS; the stand-in for AgentCore."""
    from moto import mock_aws

    from main.infra import Clients
    from tests.fakes import FakeAgentCore

    with mock_aws():
        c = Clients.for_region("ca-central-1")
        c.agentcore = FakeAgentCore()
        yield c

