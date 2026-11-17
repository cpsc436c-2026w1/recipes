"""Live updates for the recipe: location pings from drivers' phones.

Each update carries its own id and the driver it is about. Its event time (when the phone took the reading) is set by
the producer at the moment it sends, minus a simulated phone delay. Most phones send within a fraction of a second;
every `late_every`-th update comes from a phone that was offline and sends a reading it held for a while.
Both delays are assumed values for the simulation, not measured: README "Decisions".

To use your own data instead, `read_jsonl` reads a file of JSON lines (for example a stream-sim stream file).
"""

import json
import random
from datetime import datetime, timezone


def iso_ms(epoch_ms: int) -> str:
    """Epoch milliseconds as an ISO 8601 UTC string, for example 2026-11-12T18:00:00.123Z."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ms(text: str) -> int:
    """ISO 8601 string (with Z or an offset) back to epoch milliseconds."""
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def make_updates(n=300, seed=7, drivers=20, zones=(161, 162, 164, 186), late_every=10,
                 phone_delay_ms=(50, 400), offline_s=(30, 120)):
    """n updates, each with the phone delay the producer subtracts from the send time to get its event time."""
    rng = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        driver = f"d-{rng.randrange(1, drivers + 1):04d}"
        if late_every and i % late_every == 0:
            delay = rng.randint(offline_s[0] * 1000, offline_s[1] * 1000)
        else:
            delay = rng.randint(*phone_delay_ms)
        out.append({
            "update_id": f"u-{seed}-{i:06d}",       # unique per update: the consumer uses it to drop repeats
            "driver_id": driver,                     # partition key: one driver's updates stay in order
            "zone_id": rng.choice(zones),
            "phone_delay_ms": delay,
        })
    return out


def stamp(update: dict, sent_ms: int) -> dict:
    """The record as the phone sends it: event time = send time minus the phone's delay."""
    record = {k: v for k, v in update.items() if k != "phone_delay_ms"}
    record.setdefault("event_time", iso_ms(sent_ms - update.get("phone_delay_ms", 0)))
    record["sent_time"] = iso_ms(sent_ms)
    return record


def read_jsonl(path: str) -> list[dict]:
    """Updates from a JSON-lines file. Maps stream-sim's names (event_id, partition_key) to this recipe's."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            data = row.get("data", {})
            out.append({
                "update_id": row.get("update_id") or row["event_id"],
                "driver_id": row.get("driver_id") or row.get("partition_key") or data.get("driver_id"),
                "zone_id": row.get("zone_id") or data.get("zone_id", 0),
                "event_time": row["event_time"],     # kept as recorded by the device
            })
    return out
