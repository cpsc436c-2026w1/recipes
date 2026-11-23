import pytest

# The variables simulated-runs' helpers read (scripts/tags.py, scripts/lake.py): a fixed test team and owner, no
# purpose tag, and a ledger of the test's own, so no test writes the folder's state/.
HELPER_ENV = {"TEAM": "recipe-test", "OWNER": "tester", "AWS_EC2_METADATA_DISABLED": "true"}


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch, tmp_path):
    """Fake credentials and a test prefix: no test can reach a real account."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test",
                 **HELPER_ENV}.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "BUCKET", "LAG_LIMIT_SECONDS", "BUFFER_SECONDS", "PURPOSE",
              "KB_MCP_TOKEN", "COURSE_MCP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))


@pytest.fixture
def aws():
    """moto for every AWS call (simulated-runs' helper tests use it)."""
    from moto import mock_aws
    with mock_aws():
        import boto3
        yield boto3.session.Session(region_name="ca-central-1")
