"""experiments/latency.py: the join on event_id, the first send, and p95 by the nearest rank."""
import importlib.util
import json
import math
from pathlib import Path

spec = importlib.util.spec_from_file_location("latency", Path(__file__).parent.parent / "experiments" / "latency.py")
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)


def test_nearest_rank_of_2511_is_the_2386th():
    values = list(range(1, 2512))
    assert latency.nearest_rank(values, 0.95) == 2386


def test_join_uses_the_first_send_and_counts_unseen_as_infinite(tmp_path):
    sent = [{"delivery_id": "r-dl-1", "event_id": "r-ev-1", "sent_time": "2026-11-23T09:00:00.000Z", "result": "ok"},
            {"delivery_id": "r-dl-2", "event_id": "r-ev-1", "sent_time": "2026-11-23T09:00:05.000Z", "result": "ok"},
            {"delivery_id": "r-dl-3", "event_id": "r-ev-2", "sent_time": "2026-11-23T09:00:01.000Z", "result": "ok"}]
    (tmp_path / "sent.jsonl").write_text("".join(json.dumps(x) + "\n" for x in sent))
    (tmp_path / "seen.jsonl").write_text(json.dumps({"event_id": "r-ev-1", "seen_time": "2026-11-23T09:00:07.000Z"}) + "\n")
    assert sorted(latency.latencies(tmp_path)) == [7.0, math.inf]
