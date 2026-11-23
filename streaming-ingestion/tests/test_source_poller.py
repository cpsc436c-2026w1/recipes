"""ext-source-poller: the dates a run reads, the conditional claim on its own, the slot states, the HEAD path for a
feed without a manifest, and latency.py's arithmetic. Offline; moto for DynamoDB."""
import importlib.util
import io
import math
import os
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from moto import mock_aws

import poller
import slots

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("source_poller_latency", ROOT / "ext-source-poller" / "latency.py")
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)

UTC = timezone.utc


def t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def test_a_run_reads_two_dates_and_three_in_the_first_hour_and_a_quarter_after_midnight():
    assert poller.query_dates(t("2026-11-23T08:45:00Z")) == ["2026-11-22", "2026-11-23"]
    assert poller.query_dates(t("2026-11-23T00:10:00Z")) == ["2026-11-21", "2026-11-22", "2026-11-23"]
    assert poller.query_dates(t("2026-11-23T01:14:00Z")) == ["2026-11-21", "2026-11-22", "2026-11-23"]
    assert poller.query_dates(t("2026-11-23T01:15:00Z")) == ["2026-11-22", "2026-11-23"]


def _slots_table():
    ddb = boto3.client("dynamodb", region_name="ca-central-1")
    ddb.create_table(TableName="s", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "slot_day", "KeyType": "HASH"},
                                {"AttributeName": "slot_start", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "slot_day", "AttributeType": "S"},
                                           {"AttributeName": "slot_start", "AttributeType": "S"}])
    return ddb


@mock_aws
def test_two_claims_of_one_slot_one_wins():
    """Without the run log's lock (two runs that both passed it): the conditional put lets one writer win."""
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    ddb = _slots_table()
    now = t("2026-11-22T08:45:00Z")
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now, None, None) is True
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now, None, None) is False
    # a claim 15 min old is not yet stale; one older than 15 min is taken over
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now + timedelta(minutes=15), None, None) is False
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now + timedelta(minutes=15, seconds=1), None, None) is True
    poller.mark(ddb, "s", "2026-11-22T08:00Z", "failed", error_text="x")
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now + timedelta(minutes=16), None, None) is True
    poller.mark(ddb, "s", "2026-11-22T08:00Z", "loaded", record_total=3)
    assert poller.claim(ddb, "s", "2026-11-22T08:00Z", now + timedelta(hours=2), None, None) is False


def test_a_slot_without_an_item_is_not_due_for_30_minutes_then_missing():
    day = slots.slot_states([], "2026-11-22", t("2026-11-22T10:00:00Z"))
    by = {s["slot_start"]: s["slot_state"] for s in day}
    assert len(day) == 96 and by["2026-11-22T09:15Z"] == "not due" and by["2026-11-22T09:00Z"] == "missing"
    by = {s["slot_start"]: s["slot_state"] for s in slots.slot_states([], "2026-11-22", t("2026-11-22T10:00:01Z"))}
    assert by["2026-11-22T09:15Z"] == "missing"


def test_record_total_only_for_loaded_and_late():
    items = [{"slot_start": "2026-11-22T00:00Z", "slot_state": "loaded", "record_total": "201"},
             {"slot_start": "2026-11-22T00:15Z", "slot_state": "failed", "record_total": "5", "error_text": "x"}]
    day = slots.slot_states(items, "2026-11-22", t("2026-11-23T00:46:00Z"))
    assert day[0] == {"slot_start": "2026-11-22T00:00Z", "slot_state": "loaded", "record_total": 201}
    assert day[1] == {"slot_start": "2026-11-22T00:15Z", "slot_state": "failed"}


def test_a_day_that_is_not_a_date_gets_400():
    for day in ("yesterday", "2026-11-31", "2026-11-2", ""):
        assert slots.get_slots({"day": day}, None, "s")[0] == 400


def test_a_feed_without_a_manifest_is_asked_with_head():
    asked = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        asked.append((req.get_method(), req.full_url))
        if req.full_url.endswith("0830.csv"):
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        return Resp(b"")

    feed = poller.Feed("https://data.example.org/feed", opener=opener, pattern="%Y%m%d%H%M.csv")
    listed = feed.listed(t("2026-11-22T09:00:00Z"))
    assert all(m == "HEAD" for m, _ in asked) and len(asked) == 100         # 25 hours of slots
    names = [e["slot"] for e in listed]
    assert "2026-11-22T08:30Z" not in names and names[-1] == "2026-11-22T08:45Z"


def test_records_name_the_line_that_is_cut():
    import pytest

    good = b'{"event_type": "trip_completed", "event_id": "e", "trip_id": "t", "driver_id": "d", ' \
           b'"dropoff_zone_id": 48, "event_time": "2026-11-22T08:00:01Z"}\n'
    with pytest.raises(ValueError, match="line 2"):
        poller.records_of(good + good[:40], t("2026-11-22T08:30:00Z"))
    (r,) = poller.records_of(good, t("2026-11-22T08:30:00Z"))
    assert r == {"update_id": "e", "trip_id": "t", "driver_id": "d", "zone_id": 48,
                 "event_time": "2026-11-22T08:00:01Z", "sent_time": "2026-11-22T08:30:00.000Z"}


def test_p95_of_95_is_the_91st_and_a_slot_never_loaded_is_over():
    published = [{"slot": f"s{i:02d}", "published_time": "2026-11-22T00:17:00Z"} for i in range(95)]
    lines = [{"slot": f"s{i:02d}", "slot_state": "loaded",
              "last_accepted": (t("2026-11-22T00:17:00Z") + timedelta(minutes=i % 15)).isoformat()}
             for i in range(94)]
    got = latency.latencies(published, lines)
    assert math.isinf(got["s94"]) and len(got) == 95
    assert latency.nearest_rank(list(range(1, 96))) == 91


@mock_aws
def test_two_runs_started_in_the_same_second_keep_a_run_log_item_each(monkeypatch, tmp_path):
    """Seen live: a scheduled run and one started by hand at 06:29:24 left one run-log item between them."""
    import run_log

    monkeypatch.setenv("PREFIX", "recipe-test")
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    s = boto3.Session(region_name="ca-central-1")
    table = run_log.ensure_run_log(s)
    ddb = s.client("dynamodb")
    now = datetime(2026, 11, 23, 6, 29, 24, tzinfo=timezone.utc)
    for request in ("req-a", "req-b"):
        assert poller.take_lock(ddb, table, now)
        poller.release_lock(ddb, table, now, {"loaded": 0, "ended_at": poller.stamp(now)},
                            f"{poller.stamp(now)}-{request}")
    ids = sorted(i["run_id"]["S"] for i in ddb.scan(TableName=table)["Items"])
    assert ids == ["2026-11-23T06:29:24Z-req-a", "2026-11-23T06:29:24Z-req-b"]
