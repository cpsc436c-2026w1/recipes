"""The tests of simulated-runs' shared helpers, copied with them unchanged: they expect its test prefix."""
import pytest


@pytest.fixture(autouse=True)
def _helper_prefix(monkeypatch):
    monkeypatch.setenv("PREFIX", "recipe-test")
    monkeypatch.setenv("TEAM", "recipe-test")
    monkeypatch.delenv("AWS_REGION", raising=False)
