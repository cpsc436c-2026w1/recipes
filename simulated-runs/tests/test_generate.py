"""generate.py and truth.py: the counts the README's Data section states, the _truth/ layout, and the same bytes from
the same seed."""
import filecmp
import json

import pytest

import generate
import truth


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    folder = tmp_path_factory.mktemp("data")
    for name in ("SIM01", "SIM02", "SIM03", "SIM04"):
        generate.generate(name, out=folder)
    return folder


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def deliveries(folder):
    return [json.loads(x) for x in generate.delivery_lines(folder)]


def files(folder):
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


@pytest.mark.parametrize("sid, events, deliveries, t_events, t_trips, zones, completed", [
    ("SIM01", 2511, 2515, 2511, 837, {"48": 304, "161": 191, "230": 342}, 837),
    ("SIM03", 60, 60, 60, 20, {"161": 20}, 20),
    ("SIM04", 2513, 2508, 2504, 834, {"48": 304, "161": 188, "230": 342}, 834)])
def test_counts(out, sid, events, deliveries, t_events, t_trips, zones, completed):
    assert len(lines(out / sid / "_truth" / "events.jsonl")) == events
    assert len(generate.delivery_lines(out / sid)) == deliveries
    t = truth.answer(out / sid)
    assert (t["events"], t["trips"], t["requested_per_zone"], t["completed"]) == (t_events, t_trips, zones, completed)


def test_the_cp1_layout(out):
    """Deliveries by day and hour of receipt under trip-events/, the events and the faults under _truth/, and no
    stored answer."""
    assert files(out / "SIM01") == ["_truth/events.jsonl", "_truth/faults.jsonl",
                                    "trip-events/dt=2026-11-23/hour=09/deliveries.jsonl",
                                    "trip-events/dt=2026-11-23/hour=10/deliveries.jsonl",
                                    "trip-events/dt=2026-11-23/hour=11/deliveries.jsonl"]
    for path in generate.delivery_files(out / "SIM04"):
        hour = path.parent.name.removeprefix("hour=")
        assert {d["received_time"][11:13] for d in lines(path)} == {hour}, path
    received = [d["received_time"] for d in deliveries(out / "SIM04")]
    assert received == sorted(received)


def test_each_fault_is_one_line_with_its_ids(out):
    faults = truth.read_faults(out / "SIM04")
    assert {f["fault"] for f in faults} == {"twice", "late", "never", "changed"}
    assert all(set(f) == {"fault", "delivery_id", "event_id"} for f in faults if f["fault"] in ("twice", "late"))
    assert all(set(f) == {"fault", "event_id", "trip_id"} for f in faults if f["fault"] in ("never", "changed"))
    assert truth.read_faults(out / "SIM02") == [{"fault": "broken", "line": 4, "field": "tip"},
                                                {"fault": "broken", "line": 7, "field": "event_time"},
                                                {"fault": "broken", "line": 9, "field": "trip_id"}]
    assert truth.answer(out / "SIM02")["events"] == 7


def test_generating_again_removes_what_was_there(out, tmp_path):
    (tmp_path / "SIM03" / "trip-events" / "dt=2020-01-01" / "hour=00").mkdir(parents=True)
    (tmp_path / "SIM03" / "trip-events" / "dt=2020-01-01" / "hour=00" / "deliveries.jsonl").write_text("{}\n")
    (tmp_path / "SIM03" / "truth.json").write_text("{}")
    generate.generate("SIM03", out=tmp_path)
    assert files(tmp_path / "SIM03") == files(out / "SIM03")


def test_same_seed_same_bytes(out, tmp_path):
    generate.generate("SIM04", 436, tmp_path)
    names = files(out / "SIM04")
    assert files(tmp_path / "SIM04") == names and len(names) == 5
    assert filecmp.cmpfiles(out / "SIM04", tmp_path / "SIM04", names, shallow=False)[0] == names


def test_another_seed_other_faults(out, tmp_path):
    generate.generate("SIM01", 437, tmp_path)
    assert ((tmp_path / "SIM01" / "_truth" / "faults.jsonl").read_text()
            != (out / "SIM01" / "_truth" / "faults.jsonl").read_text())


def test_twice_is_a_second_delivery_of_the_same_event(out):
    ds = {d["delivery_id"]: d for d in deliveries(out / "SIM01")}
    faults = truth.by_kind(truth.read_faults(out / "SIM01"))
    for did in faults["twice"]:
        same = [d for d in ds.values() if d["event_id"] == ds[did]["event_id"]]
        assert len(same) == 2 and ds[did]["received_time"] == max(d["received_time"] for d in same)


def test_never_events_are_not_delivered_and_changed_are_later(out):
    faults = truth.by_kind(truth.read_faults(out / "SIM04"))
    delivered = {d["event_id"] for d in deliveries(out / "SIM04")}
    assert not set(faults["never"]) & delivered and set(faults["changed"]) <= delivered
    events = {e["event_id"]: e for e in lines(out / "SIM04" / "_truth" / "events.jsonl")}
    for cid in faults["changed"]:
        c = events[cid]
        first = [e for e in events.values() if e["trip_id"] == c["trip_id"] and e["event_type"] == "trip_completed"
                 and e["event_id"] != cid][0]
        assert c["event_time"] > first["event_time"] and round(c["fare"] - first["fare"], 2) == 5.0


def test_bad_lines_are_broken_where_the_readme_says(out):
    text = (out / "SIM02" / "_truth" / "events.jsonl").read_text().splitlines()
    assert len(text) == 10
    assert "tip" in json.loads(text[3]) and not json.loads(text[6])["event_time"].endswith("Z")
    assert "trip_id" not in json.loads(text[8])


def test_truth_counts_late_events_and_leaves_out_never():
    events = [{"event_id": "e1", "trip_id": "t1", "event_type": "trip_requested", "pickup_zone_id": 48,
               "event_time": "2026-11-23T09:00:00.000Z"},
              {"event_id": "e2", "trip_id": "t2", "event_type": "trip_requested", "pickup_zone_id": 48,
               "event_time": "2026-11-23T09:00:00.000Z"}]
    t = truth.compute(events, [{"fault": "never", "event_id": "e2", "trip_id": "t2"},
                               {"fault": "late", "delivery_id": "dl-1", "event_id": "e1"}])
    assert t["events"] == 1 and t["requested_per_zone"] == {"48": 1}
    assert t["requested_per_zone_day"] == {"2026-11-23": {"48": 1}}


def test_an_unknown_data_set_is_refused():
    with pytest.raises(SystemExit):
        generate.generate("SIM09")
