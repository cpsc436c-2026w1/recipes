"""Offline fixtures: data in a temporary folder, the stub model, fake AWS credentials for moto."""

from __future__ import annotations

import os

import pytest

from main.config import ROOT
from main.data import data_version, make_data
from main.engine import Engine
from main.model import StubModel


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    # moto intercepts every boto3 call; these values make sure nothing could reach a real account.
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    yield


@pytest.fixture(scope="session")
def paths(tmp_path_factory):
    return make_data(tmp_path_factory.mktemp("data"))


@pytest.fixture
def engine(paths):
    return Engine(paths)


@pytest.fixture
def version(paths):
    return data_version(paths)


@pytest.fixture
def stub():
    return StubModel.from_file(ROOT / "main" / "stub_replies.json")


@pytest.fixture
def plain_duckdb(paths):
    """An unlocked connection over the same files, to compute the expected numbers independently."""
    import duckdb

    con = duckdb.connect()
    for t, p in paths.items():
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    return con


assert os  # imported for monkeypatch users
