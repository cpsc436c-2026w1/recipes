"""A simulated half hour of updates with fixed event times, sent in the order they would arrive.

Unlike main/updates.py, the event times are set here, not at send time, so a run of a few seconds covers several
5-minute windows. Most updates arrive in event-time order; every `late_every`-th arrives `late_by_s` seconds after its
event time (a phone that was offline), so it is sent among later updates. The delays are assumed, for the simulation.
`truth` is what a count with no late records lost would give, to check the windows against.
"""

import random
from collections import Counter
from datetime import datetime, timezone

from windows import WINDOW_MS, _iso, _ms, window_of


def make_timeline(minutes=30, per_minute=20, start_ms=None, seed=11, zones=(161, 162, 164, 186),
                  late_every=15, late_by_s=(30, 600)):
    rng = random.Random(seed)
    if start_ms is None:
        start_ms = 1_800_000_000_000                         # a fixed instant on a window boundary
    start_ms -= start_ms % WINDOW_MS
    n = minutes * per_minute
    rows = []
    for i in range(n):
        event_ms = start_ms + int(i * 60_000 / per_minute) + rng.randint(0, 2_000)
        late_ms = rng.randint(late_by_s[0] * 1000, late_by_s[1] * 1000) if late_every and i % late_every == 7 else 0
        rows.append((event_ms + late_ms, {
            "update_id": f"w-{seed}-{i:06d}",
            "driver_id": f"d-{rng.randrange(1, 21):04d}",
            "zone_id": rng.choice(zones),
            "event_time": _iso_ms(event_ms),
            "late_by_s": late_ms // 1000,
        }))
    rows.sort(key=lambda r: r[0])                              # arrival order
    return [u for _, u in rows]


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def truth(updates) -> Counter:
    """Updates per (zone, window start) by event time: what every window should hold if nothing were dropped."""
    return Counter((u["zone_id"], _iso(window_of(_ms(u["event_time"]))[0])) for u in updates)
