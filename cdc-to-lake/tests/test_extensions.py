"""The three extensions: lag alarm, rollback and schema change, maintenance."""

import importlib.util
import json
from pathlib import Path

import boto3
from moto import mock_aws

from main.config import Settings
from main.lake import LocalLake

ROOT = Path(__file__).resolve().parent.parent


def load(folder: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / folder / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lag = load("ext-lag-alarm", "lag")
rollback = load("ext-rollback-or-schema", "rollback")
schema_change = load("ext-rollback-or-schema", "schema_change")
maintenance = load("ext-maintenance", "maintenance")


# --- lag and alarm ---

def test_lag_alarm_fires_only_past_the_limit():
    alarm = lag.alarm_definitions(Settings())[0]
    assert alarm["Threshold"] == 300.0
    assert lag.alarm_state(alarm, [62.0]) == "OK"
    assert lag.alarm_state(alarm, [462.0]) == "ALARM"
    assert lag.alarm_state(alarm, []) == "INSUFFICIENT_DATA"


def test_lag_report_has_both_probes_and_a_freshness_value(tmp_path):
    out = lag.run_local(Settings(), tmp_path)
    assert "alarm OK" in out and "alarm ALARM" in out
    doc = json.loads(out[out.index("{"):])
    assert doc["data_last_updated"] and doc["change_to_queryable_s"] > 300


def test_alarms_are_accepted_by_cloudwatch():
    with mock_aws():
        cw = boto3.client("cloudwatch")
        names = lag.put_alarms(cw, Settings())
        assert sorted(a["AlarmName"] for a in cw.describe_alarms()["MetricAlarms"]) == sorted(names)


# --- rollback and schema change ---

def fill(tmp_path):
    from main.run import run_local

    run_local(Settings(), tmp_path, buffer_s=0.0)
    return LocalLake(tmp_path)


def test_rollback_restores_the_earlier_row_count(tmp_path):
    lake = fill(tmp_path)
    before = rollback.stats(lake.table)
    bad = lake.append(rollback.bad_backfill(lake.table, now_us=1))
    assert rollback.stats(lake.table)["avg_fare"] > 10 * before["avg_fare"]  # what the check sees
    rollback.restore(lake.table, bad)
    assert rollback.stats(lake.table) == before


def test_rollback_keeps_good_commits_made_after_the_bad_one(tmp_path):
    lake = fill(tmp_path)
    before = rollback.stats(lake.table)["changes"]
    bad = lake.append(rollback.bad_backfill(lake.table, now_us=1))
    lake.append([{"trip_id": "t-9", "op": "INSERT", "change_time": 2, "arrival_time": 3, "seq": "9" * 40,
                  "status": "requested", "pickup_zone": "Fairview", "fare_quote": 9.0, "driver_id": None}])
    result = rollback.restore(lake.table, bad)
    assert result["good_rows_kept"] == 1
    assert rollback.stats(lake.table)["changes"] == before + 1


def test_old_queries_survive_an_added_column(tmp_path):
    out = schema_change.run_local(Settings(), tmp_path)
    assert "old query, same answer for the old trips: True" in out
    assert "t-005=card" in out and "t-001=None" in out
    assert schema_change.alter_sql(Settings()) == "ALTER TABLE test_cdc.trip_changes ADD COLUMNS (payment_type string)"


# --- maintenance ---

def test_compaction_reads_fewer_files_and_bytes_with_the_same_answer(tmp_path):
    lake = LocalLake(tmp_path)
    import datetime as dt

    maintenance.stream_like_commits(lake, 30, 4, dt.datetime(2026, 11, 12, tzinfo=dt.UTC))
    before = maintenance.measure_local(lake)
    maintenance.compact_local(lake)
    maintenance.expire_local(lake, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))
    after = maintenance.measure_local(lake)
    assert after["files"] < before["files"] and after["data_scanned"] < before["data_scanned"]
    assert after["snapshots"] < before["snapshots"]
    assert sorted(map(str, after["result"])) == sorted(map(str, before["result"]))


def test_schedules_are_valid_for_eventbridge_scheduler():
    fn = "arn:aws:lambda:ca-central-1:111122223333:function:test-compact-days"
    defs = maintenance.schedule_definitions("test", fn, "arn:aws:iam::111122223333:role/scheduler")
    assert [json.loads(d["Target"]["Input"]) for d in defs] == [{"mode": "compact"}, {"mode": "vacuum"}]
    with mock_aws():
        sch = boto3.client("scheduler")
        sch.create_schedule_group(Name="test-maintenance-compaction")
        for d in defs:
            sch.create_schedule(**d)
        assert sorted(x["Name"] for x in sch.list_schedules(GroupName="test-maintenance-compaction")["Schedules"]) \
            == ["test-compact-schedule", "test-lake-vacuum"]


# --- rollback on AWS: the credentials PyIceberg gets ---

class _Frozen:
    access_key, secret_key, token = "AKIDEXAMPLE", "not-a-real-secret", "a-session-token"


class _Session:
    """Stands in for boto3.Session: what it resolved, from whichever source (profile, instance, environment)."""

    def __init__(self, creds=_Frozen):
        self._creds = creds

    def get_credentials(self):
        return None if self._creds is None else self

    def get_frozen_credentials(self):
        return self._creds


def test_rollback_hands_pyiceberg_the_credentials_boto3_resolved():
    """With only the region given, pyarrow's S3 looked for credentials on its own
    and got ACCESS_DENIED under a profile that takes them from the EC2 instance."""
    props = rollback.catalog_properties(Settings(), session=_Session())
    assert props == {"client.region": "ca-central-1", "client.access-key-id": "AKIDEXAMPLE",
                     "client.secret-access-key": "not-a-real-secret", "client.session-token": "a-session-token"}


def test_rollback_passes_those_properties_to_the_catalog(monkeypatch):
    import boto3
    import pyiceberg.catalog.glue as glue_module

    seen = {}

    class Stop(Exception):
        pass

    class FakeCatalog:
        def __init__(self, name, **properties):
            seen.update(properties)

        def load_table(self, identifier):
            seen["table"] = identifier
            raise Stop

    monkeypatch.setattr(glue_module, "GlueCatalog", FakeCatalog)
    monkeypatch.setattr(boto3, "Session", lambda **kw: _Session())
    try:
        rollback.run_aws(Settings())
    except Stop:
        pass
    assert seen["client.access-key-id"] == "AKIDEXAMPLE" and seen["client.session-token"] == "a-session-token"
    assert seen["client.region"] == "ca-central-1" and seen["table"] == "test_cdc.trip_changes"


def test_the_properties_are_the_ones_pyiceberg_reads_for_glue_and_for_s3():
    import pyiceberg.io as io

    props = rollback.catalog_properties(Settings(), session=_Session())
    assert {io.AWS_REGION, io.AWS_ACCESS_KEY_ID, io.AWS_SECRET_ACCESS_KEY, io.AWS_SESSION_TOKEN} == set(props)


def test_rollback_without_resolved_credentials_gives_the_region_only():
    assert rollback.catalog_properties(Settings(), session=_Session(None)) == {"client.region": "ca-central-1"}


def test_long_lived_keys_have_no_session_token_property():
    class Keys:
        access_key, secret_key, token = "AKIDEXAMPLE", "not-a-real-secret", None
    assert "client.session-token" not in rollback.catalog_properties(Settings(), session=_Session(Keys))
