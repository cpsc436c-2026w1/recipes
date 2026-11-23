"""Offline tests: moto plays AWS, DuckDB plays Athena, a stub plays the model. No account, no token, no cost."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # clean_up.py lives in the recipe folder

from skeleton.cli import add_rep_paths
from skeleton.offline import fake_aws

add_rep_paths()

AS_OF = datetime(2026, 11, 12, 16, 0, 30, tzinfo=timezone.utc)   # a fixed "now", so every count is reproducible


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch, tmp_path):
    """No test depends on the caller's profile: a profile set in the shell could point a test at a real account.
    scripts/tags.py writes the test's own ledger, never the folder's state/."""
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


@pytest.fixture
def as_of():
    return AS_OF


@pytest.fixture
def aws(monkeypatch):
    """moto in this process, and no real waiting: sleeps return at once."""
    import skeleton.aws
    monkeypatch.setattr(skeleton.aws.time, "sleep", lambda s: None)
    monkeypatch.delenv("FIREHOSE_ROLE_ARN", raising=False)
    monkeypatch.delenv("API_ROLE_ARN", raising=False)
    with fake_aws():
        import boto3
        yield boto3.Session(region_name="ca-central-1")


@pytest.fixture
def local_root(tmp_path):
    return tmp_path / "lake"
