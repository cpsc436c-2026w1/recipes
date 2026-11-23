"""tags.py: the tag shapes each service takes, and the ledger's last-line-wins state."""
import json

import pytest

import tags


def test_list_shape_for_most_services():
    assert tags.tags("s3") == [{"Key": "Project", "Value": "436c"}, {"Key": "Team", "Value": "recipe-test"},
                               {"Key": "Owner", "Value": "tester"}]


@pytest.mark.parametrize("service", ["lambda", "logs", "sqs", "kinesis", "glue"])
def test_map_shape(service):
    assert tags.tags(service) == {"Project": "436c", "Team": "recipe-test", "Owner": "tester"}


def test_purpose_only_when_set(monkeypatch):
    monkeypatch.setenv("PURPOSE", "recipe")
    assert tags.tags("lambda")["purpose"] == "recipe"
    assert tags.tags("ecs")[-1] == {"key": "purpose", "value": "recipe"}


def test_team_defaults_to_prefix(monkeypatch):
    monkeypatch.delenv("TEAM")
    assert tags.tag_values()["Team"] == "recipe-test"


def test_record_appends_and_last_state_wins(tmp_path):
    tags.record("s3-bucket", "b", "created", arn="arn:aws:s3:::b", script="lake.py up")
    tags.record("s3-bucket", "b", "deleted")
    rows = tags.ledger_rows()
    assert [r["state"] for r in rows] == ["created", "deleted"]
    assert rows[0]["prefix"] == "recipe-test" and rows[0]["arn"] == "arn:aws:s3:::b"
    assert tags.recorded("s3-bucket", "b") == "deleted"
    assert tags.recorded("s3-bucket", "other") is None
    assert all(json.loads(line) for line in tags.ledger_path().read_text().splitlines())


def test_record_refuses_an_unknown_state():
    with pytest.raises(ValueError):
        tags.record("s3-bucket", "b", "made")
