"""Offline only: fake credentials, no profile, no instance metadata, moto for every AWS call, a ledger of the test's
own, so no test can reach a real account or write the folder's state/."""
import pytest

FAKE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe-test",
        "TEAM": "recipe-test", "OWNER": "tester"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "PURPOSE", "ENGINE", "ETL_MIN_GAP_MIN", "MAX_RUNS_PER_DAY",
           "ETL_FAULT", "ETL_FAULT_TIMEOUT_MIN", "START_ON_STAGE", "PIPELINE_TABLE", "CHECK_TZ",
           "CHECK_CRON", "CHECK_DELAY_MIN", "CHECK_TARGET", "CHECK_QUERY", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN")


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    for k in NOT_SET:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))


@pytest.fixture
def aws():
    from moto import mock_aws
    with mock_aws():
        import boto3
        yield boto3.session.Session(region_name="ca-central-1")
