import pytest

from main.config import load_products, load_queries
from main.embed import StubEmbedder
from main.search import LocalSearch


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture(scope="session")
def search():
    return LocalSearch(load_products(), StubEmbedder())


@pytest.fixture(scope="session")
def queries():
    return load_queries()
