"""dashboard-chat's clean_up.py from the command line: a bare run (or --dry-run) lists and deletes nothing; --yes
deletes. The live run of 2026-11-23 ran it bare, as if it were the plan, and it deleted the traces table."""
from __future__ import annotations

import sys

import boto3
import pytest
from moto import mock_aws

import clean_up as cu


@pytest.fixture
def traces(monkeypatch):
    monkeypatch.setenv("PREFIX", "clitest")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ca-central-1")
        ddb.create_table(TableName="clitest-chat-traces", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}])
        yield ddb


def _tables(ddb):
    return ddb.list_tables()["TableNames"]


@pytest.mark.parametrize("args", [[], ["--dry-run"]])
def test_without_yes_nothing_is_deleted(traces, monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["clean_up.py", *args])
    cu.main()
    assert _tables(traces) == ["clitest-chat-traces"]
    out = capsys.readouterr().out
    assert "would remove, if it exists: dynamodb table clitest-chat-traces" in out and out.rstrip().endswith("Nothing deleted. Run again with --yes to delete these.")


def test_with_yes_it_deletes(traces, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["clean_up.py", "--yes"])
    cu.main()
    assert _tables(traces) == []
    assert "removed dynamodb table clitest-chat-traces" in capsys.readouterr().out


def test_yes_and_dry_run_together_are_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["clean_up.py", "--yes", "--dry-run"])
    with pytest.raises(SystemExit):
        cu.main()
