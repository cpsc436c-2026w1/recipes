"""events.py: the models refuse what they should, and a line is written the same way every time."""
import json

import pytest

from rides.events import (DELIVERY, EVENT, LineError, TripCompleted, TripRequested, as_delivery, as_event, iso,
                          parse, read_jsonl, to_line, validate_line)

REQ = {"event_type": "trip_requested", "event_id": "ev-1", "trip_id": "t-1", "pickup_zone_id": 161,
       "event_time": "2026-11-23T09:00:00.000Z"}


def test_time_is_fixed_width_utc():
    assert iso(parse("2026-11-23T01:00:00-08:00")) == "2026-11-23T09:00:00.000Z"
    with pytest.raises(ValueError):
        parse("2026-11-23T09:00:00")


def test_a_line_round_trips_with_sorted_keys():
    e = EVENT.validate_python(REQ)
    assert isinstance(e, TripRequested)
    assert to_line(e) == json.dumps(REQ, sort_keys=True)


@pytest.mark.parametrize("change, field", [({"tip": 1.0}, "tip"), ({"event_time": "2026-11-23T09:00:00"}, "event_time"),
                                          ({"trip_id": None}, "trip_id")])
def test_a_bad_line_names_file_line_and_field(change, field):
    data = {**REQ, **change}
    data = {k: v for k, v in data.items() if v is not None}
    with pytest.raises(LineError) as err:
        validate_line(json.dumps(data), EVENT, "events.jsonl", 4)
    assert (err.value.file, err.value.line, err.value.field) == ("events.jsonl", 4, field)
    assert "events.jsonl, line 4, field " + field in str(err.value)


def test_unknown_event_type_is_refused():
    with pytest.raises(LineError) as err:
        validate_line(json.dumps({**REQ, "event_type": "trip_cancelled"}), EVENT)
    assert err.value.field == "event_type"


def test_delivery_is_the_event_plus_two_fields():
    e = EVENT.validate_python(REQ)
    d = as_delivery(e, "dl-1", parse("2026-11-23T09:00:01Z"))
    line = json.loads(to_line(d))
    assert line == {**REQ, "delivery_id": "dl-1", "received_time": "2026-11-23T09:00:01.000Z"}
    assert as_event(d) == e
    with pytest.raises(LineError):          # a delivery without its received_time
        validate_line(json.dumps({**REQ, "delivery_id": "dl-1"}), DELIVERY)


def test_completion_carries_event_id_and_fare():
    c = EVENT.validate_python({"event_type": "trip_completed", "event_id": "ev-3", "trip_id": "t-1",
                               "driver_id": "d-1", "dropoff_zone_id": 230, "trip_miles": 1.2, "trip_time_s": 400,
                               "fare": 12.5, "driver_pay": 9.0, "event_time": "2026-11-23T09:10:00Z"})
    assert isinstance(c, TripCompleted) and c.event_id == "ev-3"


def test_read_jsonl_stops_at_the_first_bad_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(REQ) + "\n" + json.dumps({**REQ, "tip": 1}) + "\n")
    with pytest.raises(LineError) as err:
        read_jsonl(path)
    assert err.value.line == 2
