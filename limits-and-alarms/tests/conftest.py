"""Offline fixtures: moto stands in for AWS, with fake credentials."""

from __future__ import annotations

import pytest
from moto import mock_aws

from config import Settings


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "PREFIX": "test"}.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "ALARM_EMAIL", "LAMBDA_ROLE_ARN"):
        monkeypatch.delenv(k, raising=False)
    with mock_aws():
        yield


@pytest.fixture
def s():
    return Settings()


@pytest.fixture
def deployed(s):
    """Everything main/run.py sets up, in moto."""
    import run

    c = run.clients(s)
    lines, topic, names = run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    return {"c": c, "lines": lines, "topic": topic, "names": names}


@pytest.fixture(autouse=True)
def _budget_tags(monkeypatch):
    """moto keeps no tags on a budget and has no ListTagsForResource for AWS Budgets; this stand-in adds both.

    AWS Budgets stores the ResourceTags of CreateBudget and returns them from ListTagsForResource on the budget's
    ARN (AWS Budgets API Reference, CreateBudget and ListTagsForResource)."""
    import json

    from moto.budgets.responses import BudgetsResponse

    tags: dict[str, list] = {}
    create, delete = BudgetsResponse.create_budget, BudgetsResponse.delete_budget

    def arn(self, name):
        return f"arn:aws:budgets::{self._get_param('AccountId')}:budget/{name}"

    def create_budget(self):
        out = create(self)
        tags[arn(self, self._get_param("Budget")["BudgetName"])] = self._get_param("ResourceTags") or []
        return out

    def delete_budget(self):
        out = delete(self)
        tags.pop(arn(self, self._get_param("BudgetName")), None)
        return out

    def list_tags_for_resource(self):
        return json.dumps({"ResourceTags": tags.get(self._get_param("ResourceARN"), [])})

    monkeypatch.setattr(BudgetsResponse, "create_budget", create_budget)
    monkeypatch.setattr(BudgetsResponse, "delete_budget", delete_budget)
    monkeypatch.setattr(BudgetsResponse, "list_tags_for_resource", list_tags_for_resource, raising=False)
    yield tags
