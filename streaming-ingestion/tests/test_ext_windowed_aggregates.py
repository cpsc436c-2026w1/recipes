"""Windows by event time with a close delay: offline, against moto."""

import base64
import json

import boto3
from moto import mock_aws

import windows
from timeline import make_timeline, truth

REGION = "ca-central-1"


def load_run():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parent.parent / "ext-windowed-aggregates" / "run.py"
    spec = importlib.util.spec_from_file_location("windowed_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def event(updates, shard="shardId-000000000000"):
    return {"Records": [{"eventID": f"{shard}:{i}", "kinesis": {
        "sequenceNumber": str(i), "data": base64.b64encode(json.dumps(u).encode()).decode(),
        "approximateArrivalTimestamp": 0}} for i, u in enumerate(updates, 1)]}


def upd(uid, event_time, zone=161):
    return {"update_id": uid, "driver_id": "d-1", "zone_id": zone, "event_time": event_time}


def table():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(TableName="w", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
    return ddb


def test_window_boundaries():
    start, end = windows.window_of(windows._ms("2026-11-12T18:04:59.999Z"))
    assert windows._iso(start) == "2026-11-12T18:00:00Z" and windows._iso(end) == "2026-11-12T18:05:00Z"


@mock_aws
def test_late_record_dropped_and_counted():
    ddb = table()
    windows.handler(event([upd("a", "2026-11-12T18:01:00Z"), upd("b", "2026-11-12T18:08:00Z")]),
                    ddb=ddb, table="w", rule="drop", close_delay_s=120)
    # watermark now 18:08 - 2 min = 18:06, past the end of window 18:00-18:05
    windows.handler(event([upd("c", "2026-11-12T18:03:00Z")]), ddb=ddb, table="w", rule="drop", close_delay_s=120)
    rows, _ = windows.read_windows(ddb, "w")
    w = next(r for r in rows if r["window_start"] == "2026-11-12T18:00:00Z")
    assert (w["count"], w["late_dropped"], w["corrections"]) == (1, 1, 0)


@mock_aws
def test_record_within_close_delay_is_counted():
    ddb = table()
    windows.handler(event([upd("a", "2026-11-12T18:06:00Z")]), ddb=ddb, table="w", close_delay_s=120)
    # watermark 18:04, window 18:00-18:05 still open
    windows.handler(event([upd("b", "2026-11-12T18:04:30Z")]), ddb=ddb, table="w", close_delay_s=120)
    rows, _ = windows.read_windows(ddb, "w")
    w = next(r for r in rows if r["window_start"] == "2026-11-12T18:00:00Z")
    assert (w["count"], w["late_dropped"]) == (1, 0)


@mock_aws
def test_correct_rule_adds_and_marks_the_change():
    ddb = table()
    windows.handler(event([upd("a", "2026-11-12T18:01:00Z"), upd("b", "2026-11-12T18:09:00Z")]),
                    ddb=ddb, table="w", rule="correct", close_delay_s=60)
    windows.handler(event([upd("c", "2026-11-12T18:02:00Z")]), ddb=ddb, table="w", rule="correct", close_delay_s=60)
    rows, _ = windows.read_windows(ddb, "w")
    w = next(r for r in rows if r["window_start"] == "2026-11-12T18:00:00Z")
    assert (w["count"], w["corrections"], w["late_dropped"]) == (2, 1, 0)


@mock_aws
def test_redelivered_batch_counts_once():
    ddb = table()
    ev = event([upd("a", "2026-11-12T18:01:00Z"), upd("b", "2026-11-12T18:02:00Z")])
    windows.handler(ev, ddb=ddb, table="w")
    windows.handler(ev, ddb=ddb, table="w")
    rows, _ = windows.read_windows(ddb, "w")
    assert sum(r["count"] for r in rows) == 2


def test_timeline_has_late_arrivals():
    ups = make_timeline()
    assert sum(1 for u in ups if u["late_by_s"]) > 0
    assert sum(truth(ups).values()) == len(ups)


def test_whole_extension_local_drop_and_correct():
    run = load_run()
    for rule in ("drop", "correct"):
        rows = run.run_local(rule, 120, 25, "recipe-test")
        assert sum(r["truth"] for r in rows) == 600
        if rule == "drop":
            assert all(r["count"] + r["late_dropped"] == r["truth"] for r in rows)
            assert sum(r["late_dropped"] for r in rows) > 0
        else:
            assert all(r["count"] == r["truth"] for r in rows)
            assert sum(r["corrections"] for r in rows) > 0
        assert any(r["final"] for r in rows) and not all(r["final"] for r in rows)   # the last windows stay open
