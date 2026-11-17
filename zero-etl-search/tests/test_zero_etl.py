"""ext-zero-etl: the pipeline definition validates, the example is current, and the stand-in pipeline keeps the
newest change (external versions)."""

import boto3
import pytest
import yaml
from moto import mock_aws

import zero_etl
from main.config import Settings, load_items
from main.infra import create_table
from main.mappings import index_body
from pipeline import EXAMPLE, example_settings, render, validate
from standin_pipeline import StandInIndex, StandInPipeline, VersionConflict


def test_pipeline_is_valid_and_the_example_is_current():
    s, ex = example_settings()
    text = render(s, **ex)
    assert validate(text, s) == []
    committed = EXAMPLE.read_text().split("\n", 1)[1]  # after the header comment
    assert committed == text, "ext-zero-etl/pipeline.example.yaml is stale: run ext-zero-etl/pipeline.py --write"


def test_pipeline_mistakes_are_caught():
    s, ex = example_settings()
    doc = yaml.safe_load(render(s, **ex))
    sink = doc["recipe-items-sync"]["sink"][0]["opensearch"]
    sink["index"] = "items"
    sink.pop("document_version")
    errors = validate(yaml.safe_dump(doc), s)
    assert any("versioned index" in e for e in errors) and any("document_version" in e for e in errors)
    other_region = render(s, **{**ex, "table_arn": ex["table_arn"].replace("ca-central-1", "us-east-1")})
    assert any("this region" in e for e in validate(other_region, s))


def test_pipeline_accepted_by_the_service_model():
    """moto checks the CreatePipeline request shape (names, units); the content check is validate() above."""
    s, ex = example_settings()
    with mock_aws():
        osis = boto3.client("osis", region_name="ca-central-1")
        osis.create_pipeline(PipelineName=s.pipeline, MinUnits=1, MaxUnits=2,
                             PipelineConfigurationBody=render(s, **ex))
        assert osis.get_pipeline(PipelineName=s.pipeline)["Pipeline"]["PipelineName"] == "recipe-items-sync"


@pytest.fixture
def synced():
    s = Settings()
    with mock_aws():
        ddb = boto3.client("dynamodb")
        create_table(ddb, s, load_items())
        index = StandInIndex(s.index, index_body(s))
        pipe = StandInPipeline(ddb, boto3.client("dynamodbstreams"), s.table, index)
        pipe.export(export_version=1)
        yield s, index, pipe, boto3.resource("dynamodb").Table(s.table)


def test_update_and_delete_reach_the_index(synced):
    s, index, pipe, table = synced
    assert len(index.docs) == 8 and index.get("I04")["name"] == "Headlamp HX-200"
    zero_etl.update_item(table, zero_etl.CHANGE)
    table.delete_item(Key={"id": "I01"})
    assert pipe.poll() == 2
    assert index.get("I04")["name"] == zero_etl.CHANGE["name"] and index.get("I01") is None


def test_an_older_change_cannot_overwrite_a_newer_one():
    index = StandInIndex("items-v1", index_body(Settings()))
    index.index("I01", {"name": "new"}, version=20)
    with pytest.raises(VersionConflict):
        index.index("I01", {"name": "old"}, version=10)
    assert index.get("I01")["name"] == "new"


def test_the_extension_runs_offline():
    out = zero_etl.run_local(Settings())
    assert "mappings and pipeline definition: valid" in out and "I08 removed from the table, in the index: False" in out


class _NotFound(Exception):
    pass


class _InUse(Exception):
    pass


def test_create_pipeline_waits_for_a_pipeline_that_is_being_deleted(monkeypatch):
    class Osis:
        class exceptions:  # noqa: N801
            ResourceNotFoundException = _NotFound
            ResourceAlreadyExistsException = _InUse

        def __init__(self):
            self.status, self.polls, self.created = "DELETING", 0, 0

        def validate_pipeline(self, PipelineConfigurationBody):
            return {"isValid": True}

        def get_pipeline(self, PipelineName):
            self.polls += 1
            if self.status == "DELETING" and self.polls > 2:
                self.status = None
            if self.status is None:
                raise _NotFound()
            return {"Pipeline": {"Status": self.status}}

        def create_pipeline(self, **kw):
            if self.status is not None:
                raise _InUse()
            self.status, self.created = "ACTIVE", self.created + 1

    monkeypatch.setattr(zero_etl.time, "sleep", lambda s: None)
    s, ex = example_settings()
    osis = Osis()
    zero_etl.create_pipeline(osis, s, render(s, **ex), wait_s=5)
    assert osis.created == 1 and osis.status == "ACTIVE"
    zero_etl.create_pipeline(osis, s, render(s, **ex), wait_s=5)               # a second run: already there
    assert osis.created == 1
