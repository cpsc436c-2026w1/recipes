"""The event shape every representative reads, and three ways to get events.

One update = one keyed event:

    {"event_id": "ev-000042", "entity_id": "st-07", "event_time": "2026-11-12T15:04:05.123Z", "value": 11}

- event_id   unique per update; a resent copy keeps it, so readers can drop duplicates (README, Decisions: "duplicates")
- entity_id  the key: what the update is about (here a bike-share station; in your project a licence, a list, a sensor)
- event_time when it happened, ISO 8601 in UTC with milliseconds and a "Z": fixed width, so strings sort as times
- value      the number the update carries (here: bikes free at the station)

Change the shape here and in COLUMNS only; the Glue schema, the Parquet files and the model's system prompt are built
from COLUMNS.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

# name, Glue/Athena type, description (the description reaches the LLM in rep-genai-first's system prompt)
COLUMNS = [
    ("event_id", "string", "unique id of the update; a resent copy has the same id"),
    ("entity_id", "string", "the station the update is about, for example st-07"),
    ("event_time", "timestamp", "when the update happened, UTC"),
    ("value", "int", "bikes free at the station after the update"),
]


def iso(t: datetime) -> str:
    """UTC time as fixed-width ISO 8601 with milliseconds: 2026-11-12T15:04:05.123Z."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def generate(n: int = 600, now: datetime | None = None, hours: float = 2.0, stations: int = 20,
             duplicates: int = 5, seed: int = 436) -> list[dict]:
    """n synthetic updates spread over the `hours` before `now`, plus `duplicates` resent copies.

    Same seed and `now` give the same list. Copies are what an at-least-once producer sends after a timeout.
    """
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    out = []
    for i in range(n):
        t = now - timedelta(seconds=rng.uniform(0, hours * 3600))
        out.append({"event_id": f"ev-{i + 1:06d}", "entity_id": f"st-{rng.randint(1, stations):02d}",
                    "event_time": iso(t), "value": rng.randint(0, 30)})
    out.sort(key=lambda e: e["event_time"])
    out += [dict(e) for e in rng.sample(out, duplicates)]   # resent copies arrive last, out of order
    return out


def load_jsonl(path: str | Path) -> list[dict]:
    """Your own dataset: one JSON object per line with the four fields above."""
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    for r in rows:
        missing = [c for c, _, _ in COLUMNS if c not in r]
        if missing:
            raise ValueError(f"{path}: an event lacks {missing}: {r}")
        r["event_time"] = iso(parse(r["event_time"]))     # normalise to the fixed-width form
    return rows


def from_streamsim(path: str | Path, value_field: str) -> list[dict]:
    """Events from one stream file of 436c-stream-sim (`streams/<name>.jsonl`).

    stream-sim writes an envelope with the payload under "data"; its partition_key is the key.
    """
    out = []
    for line in Path(path).read_text().splitlines():
        e = json.loads(line)
        out.append({"event_id": e["event_id"], "entity_id": e["partition_key"],
                    "event_time": iso(parse(e["event_time"])), "value": e["data"][value_field]})
    return out


def distinct_in_window(events: list[dict], start: datetime, end: datetime) -> int:
    """The one number computed in plain Python: what every representative must agree with (tests use it)."""
    return len({e["event_id"] for e in events if start <= parse(e["event_time"]) < end})
