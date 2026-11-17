import json
from datetime import timedelta

from skeleton.events import distinct_in_window, from_streamsim, generate, iso, load_jsonl, parse
from skeleton.number import lake_sql, minutes, window


def test_generate_is_reproducible_and_has_copies(as_of):
    a, b = generate(100, now=as_of, duplicates=5), generate(100, now=as_of, duplicates=5)
    assert a == b
    assert len(a) == 105 and len({e["event_id"] for e in a}) == 100


def test_times_are_fixed_width_and_sort_as_times(as_of):
    ev = generate(50, now=as_of)
    assert {len(e["event_time"]) for e in ev} == {24}
    by_string = sorted(e["event_time"] for e in ev)
    by_time = [iso(t) for t in sorted(parse(e["event_time"]) for e in ev)]
    assert by_string == by_time


def test_window_is_sixty_whole_minutes(as_of):
    start, end = window(as_of)
    assert end == as_of.replace(second=0, microsecond=0)
    assert end - start == timedelta(hours=1)
    assert len(minutes(start, end)) == 60


def test_own_dataset_and_streamsim(tmp_path):
    own = tmp_path / "own.jsonl"
    own.write_text(json.dumps({"event_id": "a", "entity_id": "k", "event_time": "2026-11-12T15:04:05Z", "value": 1}))
    assert load_jsonl(own)[0]["event_time"] == "2026-11-12T15:04:05.000Z"
    sim = tmp_path / "pings.jsonl"
    sim.write_text(json.dumps({"event_id": "ev-1", "partition_key": "d-0022", "event_time": "2024-03-14T12:00:00.011Z",
                               "data": {"zone_id": 186}}))
    assert from_streamsim(sim, "zone_id") == [
        {"event_id": "ev-1", "entity_id": "d-0022", "event_time": "2024-03-14T12:00:00.011Z", "value": 186}]


def test_distinct_counts_copies_once(as_of):
    ev = generate(200, now=as_of, duplicates=10)
    start, end = window(as_of)
    assert distinct_in_window(ev, start, end) == distinct_in_window(ev[:200], start, end)


def test_lake_sql_prunes_by_day(as_of):
    start, end = window(as_of)
    assert f"dt >= '{start:%Y-%m-%d}'" in lake_sql(start, end)
