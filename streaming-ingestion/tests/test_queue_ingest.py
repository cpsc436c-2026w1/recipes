"""ext-queue-ingest: the consumer's standard-library check agrees with the event models, the settings run.py refuses,
the S3 key, and latency.py's arithmetic. Offline; no AWS account."""
import importlib.util
import json
import math
from pathlib import Path

import pytest
from rides.events import DELIVERY, LineError, validate_line

import queue_consumer
import stand_in

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    """By path: both new extensions have a run.py and a latency.py."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load("queue_ingest_run", "ext-queue-ingest/run.py")
latency = _load("queue_ingest_latency", "ext-queue-ingest/latency.py")

GOOD = {"event_type": "trip_completed", "event_id": "e-1", "trip_id": "t-1", "driver_id": "d-1",
        "dropoff_zone_id": 48, "trip_miles": 1.5, "trip_time_s": 400, "fare": 12.5, "driver_pay": 9.0,
        "event_time": "2026-11-23T09:00:00.000Z", "delivery_id": "dl-e-1", "received_time": "2026-11-23T09:00:01Z"}


def variants():
    yield "good", GOOD
    yield "request", {"event_type": "trip_requested", "event_id": "e-2", "trip_id": "t-2", "pickup_zone_id": 161,
                      "event_time": "2026-11-23T09:00:00Z", "delivery_id": "dl-e-2",
                      "received_time": "2026-11-23T09:00:01+00:00"}
    yield "extra field", {**GOOD, "tip": 2.0}
    yield "no trip_id", {k: v for k, v in GOOD.items() if k != "trip_id"}
    yield "time without zone", {**GOOD, "event_time": "2026-11-23T09:00:00"}
    yield "unknown kind", {**GOOD, "event_type": "trip_cancelled"}
    yield "zone as text", {**GOOD, "dropoff_zone_id": "forty-eight"}
    yield "no delivery_id", {k: v for k, v in GOOD.items() if k != "delivery_id"}


@pytest.mark.parametrize("name,line", list(variants()))
def test_the_check_and_the_models_accept_and_refuse_the_same_lines(name, line):
    text = json.dumps(line)
    try:
        validate_line(text, DELIVERY)
        models_ok = True
    except LineError:
        models_ok = False
    try:
        queue_consumer.check_delivery(text)
        check_ok = True
    except ValueError:
        check_ok = False
    assert check_ok == models_ok, name


def test_every_line_of_the_rush_hour_passes_the_check(tmp_path):
    folder = stand_in.ensure("rush-hour", tmp_path)
    lines = [x for f in sorted(folder.glob("trip-events/dt=*/hour=*/deliveries.jsonl"))
             for x in f.read_text().splitlines()]                       # the simulator's layout: by hour of receipt
    assert len(lines) == 2515
    kinds = [queue_consumer.check_delivery(x)["event_type"] for x in lines]
    assert kinds.count("trip_completed") == 837 and len(kinds) - 837 == 1678


def test_not_json_is_refused():
    with pytest.raises(ValueError, match="not JSON"):
        queue_consumer.check_delivery('{"event_type": "trip_completed", "event_id": "e-1", ')


@pytest.mark.parametrize("value,ok", [("30", True), ("45", True), ("60", True), ("0", False), ("29", False),
                                      ("61", False), ("thirty", False)])
def test_the_batching_window_is_30_to_60(value, ok):
    if ok:
        assert run.batch_window({"BATCH_WINDOW_S": value}) == int(value)
    else:
        with pytest.raises(run.SettingError, match="BATCH_WINDOW_S must be 30 to 60"):
            run.batch_window({"BATCH_WINDOW_S": value})


def test_the_batching_window_is_60_unless_set():
    assert run.batch_window({}) == 60
    assert run.settings("dynamodb", run.batch_window({}))["visibility_s"] == 240


def test_settings_give_six_timeouts_plus_the_window():
    assert run.settings("dynamodb", 30) == {"window_s": 30, "batch_size": 100, "visibility_s": 210}
    assert run.settings("dynamodb", 60) == {"window_s": 60, "batch_size": 100, "visibility_s": 240}
    assert run.settings("s3", 30) == {"window_s": 60, "batch_size": 100, "visibility_s": 240}


def test_an_object_key_is_under_the_arrival_day_and_names_the_call():
    key = queue_consumer.object_key(1795424410.5, "req-1")            # 2026-11-23T09:00:10.500Z
    assert key == "raw/trip-events/dt=2026-11-23/20261123T090010500Z-req-1.jsonl"


def test_p95_is_the_nearest_rank():
    values = list(range(1, 838))                                       # 837 values: the 796th smallest
    assert latency.nearest_rank(values) == 796


def test_a_completion_without_an_item_is_over_the_target():
    sent = [{"delivery_id": "r-dl-e-1", "sent_time": "2026-11-23T09:00:00.000Z", "result": "ok"}]
    items = [{"trip_id": "r-t-1", "delivery_id": "r-dl-e-1", "stored_time": "2026-11-23T09:00:31.250Z"}]
    got = latency.latencies(items, sent, ["t-1", "t-2"], "r")
    assert got["t-1"] == 31.25 and math.isinf(got["t-2"])
