import pytest


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    """Fake credentials and a test prefix: no test can reach a real account."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "BUCKET", "LAG_LIMIT_SECONDS", "BUFFER_SECONDS"):
        monkeypatch.delenv(k, raising=False)
