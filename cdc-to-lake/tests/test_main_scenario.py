"""Validation of the main scenario: a changed item appears in the lake query; freshness is reported."""

import zipfile
import io

import boto3
from moto import mock_aws

from main.config import Settings
from main.infra import create_function, create_table, firehose_config, function_zip
from main.lake import LocalFirehose, LocalLake
from main.run import CHANGED, REMOVED, run_local


def test_changed_item_appears_in_the_lake(tmp_path):
    out = run_local(Settings(), tmp_path, buffer_s=0.1)
    assert "rows queryable before the buffer was written: 0" in out
    lake = LocalLake(tmp_path)
    current = {r["trip_id"]: r for r in lake.query("SELECT * FROM trips_current")}
    assert current[CHANGED]["status"] == "claimed" and current[CHANGED]["driver_id"] == "d-42"
    assert REMOVED not in current
    ops = sorted(r["op"] for r in lake.query("SELECT op FROM trip_changes"))
    assert ops == ["INSERT", "INSERT", "INSERT", "MODIFY", "REMOVE"]  # the log keeps every change
    lag = lake.query("SELECT min(date_diff('second', change_time, arrival_time)) AS m FROM trip_changes")[0]["m"]
    assert lag >= 0  # nothing arrives before it changed


def test_buffer_decides_when_rows_become_queryable(tmp_path):
    now = [0.0]
    lake = LocalLake(tmp_path)
    fh = LocalFirehose(lake, interval_s=60, clock=lambda: now[0])
    fh.put_record_batch("s", [{"Data": b'{"trip_id": "a", "op": "INSERT", "change_time": 0, "arrival_time": 0,'
                                       b' "seq": "1"}'}])
    now[0] = 59
    assert not fh.tick() and lake.query("SELECT count(*) AS n FROM trip_changes")[0]["n"] == 0
    now[0] = 60
    assert fh.tick() and lake.query("SELECT count(*) AS n FROM trip_changes")[0]["n"] == 1


def test_firehose_config_is_append_only_iceberg():
    cfg = firehose_config(Settings(), "111122223333")
    assert cfg["AppendOnly"] is True
    assert cfg["CatalogConfiguration"]["CatalogARN"] == "arn:aws:glue:ca-central-1:111122223333:catalog"
    assert cfg["DestinationTableConfigurationList"][0]["DestinationTableName"] == "trip_changes"
    assert 0 <= cfg["BufferingHints"]["IntervalInSeconds"] <= 900


def test_function_package_and_stream_mapping():
    assert zipfile.ZipFile(io.BytesIO(function_zip())).namelist() == ["handler.py"]
    with mock_aws():
        iam = boto3.client("iam")
        role = iam.create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        s = Settings(lambda_role_arn=role)
        stream_arn = create_table(boto3.client("dynamodb"), s)
        create_table(boto3.client("dynamodb"), s)  # a second run keeps the table
        create_function(boto3.client("lambda"), s, stream_arn)
        create_function(boto3.client("lambda"), s, stream_arn)  # and does not add a second mapping
        maps = boto3.client("lambda").list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"]
        assert len(maps) == 1 and maps[0]["EventSourceArn"] == stream_arn
