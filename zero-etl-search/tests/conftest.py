import pytest


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    """Fake credentials and a test prefix: no test can reach a real account or the gateway."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "BUCKET", "INDEX_VERSION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LAMBDA_ROLE_ARN",
              "PIPELINE_ROLE_ARN"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _no_lambda_containers(monkeypatch):
    """moto runs a function in Docker for each item written while a stream mapping exists; the tests drive the
    handler themselves (main/standin.py), so that hook does nothing here and no test waits on Docker."""
    from moto.awslambda.models import LambdaBackend

    monkeypatch.setattr(LambdaBackend, "send_dynamodb_items", lambda self, *a, **kw: None)
