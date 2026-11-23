"""Count a simulated data set into `<PREFIX>-zone-counts` the way a counting function does, for the page to show.

    uv run python ext-dashboard-page/load_counts.py rush-hour --end-at now     # the hour ends now: a fresh page
    uv run python ext-dashboard-page/load_counts.py quiet-zone                 # times as in the file (2026-11-23)
    DRY=1 uv run python ext-dashboard-page/load_counts.py rush-hour            # what it would write

Data sets (README, Data): rush-hour (SIM01: the simulated hour of sim/data/SIM01, with its repeated and late
deliveries), quiet-zone (SIM02: each event of that hour once, without the 60 trips of zone 48 requested from 09:25
to 09:35), feed-stops (SIM03: the rush-hour deliveries without any event after 09:40:00).

Counting: each trip_requested counted once by event_id (a repeated delivery is not counted twice), in the period
of its event time; one count item per zone and period that had a request, and one `latest` item per zone, written
with a condition that keeps a newer time already there.

The deliveries go through the course replayer (sim/replay.py, `local` target) to `handle`, which is also the
function a replay can call itself: `--to local --target load_counts:handle` with ext-dashboard-page on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
sys.path[:0] = [str(RECIPE / "scripts"), str(RECIPE / "sim"), str(HERE)]
from page import floor, parse_period, period_key, stamp, utc  # noqa: E402
from tags import prefix, record, recorded, tags  # noqa: E402

SIM_END = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)    # the simulated hour is 09:00 to 10:00 UTC
DATA = HERE / "data"
SETS = {"rush-hour": "SIM01", "quiet-zone": "SIM02", "feed-stops": "SIM03"}
REGION = "ca-central-1"


def table_name() -> str:
    return os.environ.get("COUNTS_TABLE") or f"{prefix()}-zone-counts"


# ---- data sets

def _sim01() -> Path:
    import generate
    return generate.ensure("SIM01")


def build(name: str) -> str:
    """Write data/<name>/trip-events/dt=<day>/hour=<hour>/deliveries.jsonl (derived from SIM01, in the layout of
    sim/generate.py) and return the folder name replay reads."""
    import generate
    if name not in SETS:
        raise SystemExit(f"unknown data set {name}; one of {', '.join(SETS)}")
    src = _sim01()
    if name == "rush-hour":
        lines = generate.delivery_lines(src)
    elif name == "feed-stops":
        lines = [l for l in generate.delivery_lines(src) if json.loads(l)["event_time"] < "2026-11-23T09:40:00"]
    else:
        events = [json.loads(l) for l in (src / "_truth" / "events.jsonl").read_text().splitlines()]
        quiet = {e["trip_id"] for e in events if e["event_type"] == "trip_requested" and e["pickup_zone_id"] == 48
                 and "2026-11-23T09:25:00" <= e["event_time"] < "2026-11-23T09:35:00"}
        lines = [json.dumps({**e, "delivery_id": f"dq-{n:06d}", "received_time": e["event_time"]}, sort_keys=True)
                 for n, e in enumerate(events, 1) if e["trip_id"] not in quiet]
    folder = DATA / name
    shutil.rmtree(folder, ignore_errors=True)
    files: dict[Path, list[str]] = {}
    for line in lines:
        files.setdefault(generate.delivery_path(folder, utc(json.loads(line)["received_time"])), []).append(line)
    for path, chunk in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(l + "\n" for l in chunk))
    return name


# ---- counting

class Counter:
    """Distinct requests per zone and period, across every batch of one replay."""

    def __init__(self, period: timedelta):
        self.period, self.seen, self.cells, self.latest = period, set(), {}, {}

    def add(self, deliveries: list[dict]) -> set:
        touched = set()
        for d in deliveries:
            if d.get("event_type") != "trip_requested" or d["event_id"] in self.seen:
                continue
            self.seen.add(d["event_id"])
            t, zone = utc(d["event_time"]), int(d["pickup_zone_id"])
            key = (zone, period_key(floor(t, self.period), self.period))
            cell = self.cells.setdefault(key, {"requested": 0, "newest": t})
            cell["requested"] += 1
            cell["newest"] = max(cell["newest"], t)
            self.latest[zone] = max(self.latest.get(zone, t), t)
            touched.add(key)
        return touched


def write(counter: Counter, keys, ddb, table: str, log=print) -> dict:
    """Put the count items of `keys` with their totals so far, and each zone's latest item, kept when newer."""
    done = {"count_items": 0, "latest_written": 0, "latest_kept": 0}
    for zone, period in sorted(keys):
        cell = counter.cells[(zone, period)]
        ddb.put_item(TableName=table, Item={
            "zone_id": {"N": str(zone)}, "period_start": {"S": period},
            "requested": {"N": str(cell["requested"])}, "newest_event_time": {"S": stamp(cell["newest"])}})
        done["count_items"] += 1
    for zone in sorted({z for z, _ in keys}):
        t = stamp(counter.latest[zone])
        try:
            ddb.update_item(TableName=table, Key={"zone_id": {"N": str(zone)}, "period_start": {"S": "latest"}},
                            UpdateExpression="SET newest_event_time = :t",
                            ConditionExpression="attribute_not_exists(newest_event_time) OR newest_event_time < :t",
                            ExpressionAttributeValues={":t": {"S": t}})
            done["latest_written"] += 1
        except ddb.exceptions.ConditionalCheckFailedException:
            done["latest_kept"] += 1      # a newer time is there already: kept
    return done


_state: dict = {}


def handle(batch: list[dict]) -> None:
    """The replayer's local target: count one batch and write the items it changed."""
    if "counter" not in _state:
        import boto3
        from page import settings
        _state["counter"] = Counter(settings()["period"])
        _state["ddb"] = boto3.client("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", REGION))
    keys = _state["counter"].add(batch)
    if keys:
        write(_state["counter"], keys, _state["ddb"], table_name())


def ensure_table(sess, log=print) -> str:
    """Create `<PREFIX>-zone-counts` if missing (recorded `created`); a table that is there is `borrowed`."""
    ddb, name = sess.client("dynamodb"), table_name()
    try:
        t = ddb.describe_table(TableName=name)["Table"]
        keys = {k["AttributeName"]: k["KeyType"] for k in t["KeySchema"]}
        if keys != {"zone_id": "HASH", "period_start": "RANGE"}:
            raise SystemExit(f"{name} exists with the keys {keys}, not zone_id and period_start")
        if recorded("dynamodb-table", name) not in ("created", "borrowed"):
            record("dynamodb-table", name, "borrowed", arn=t["TableArn"], script="ext-dashboard-page")
        log(f"table {name}: there already, used as it is")
        return "borrowed"
    except ddb.exceptions.ResourceNotFoundException:
        pass
    made = ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", Tags=tags("dynamodb"),
                            KeySchema=[{"AttributeName": "zone_id", "KeyType": "HASH"},
                                       {"AttributeName": "period_start", "KeyType": "RANGE"}],
                            AttributeDefinitions=[{"AttributeName": "zone_id", "AttributeType": "N"},
                                                  {"AttributeName": "period_start", "AttributeType": "S"}])
    record("dynamodb-table", name, "created", arn=made["TableDescription"]["TableArn"],
           script="ext-dashboard-page", clean="clean_up.py")
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 60})
    log(f"table {name}: created")
    return "created"


def load(name: str, end_at: datetime | None, sess=None, log=print, run_id: str | None = None) -> dict:
    """Count one data set into the table through the replayer; returns the replay summary and the item counts."""
    import boto3
    import replay
    sess = sess or boto3.session.Session(region_name=REGION)
    data_set = build(name)
    deliveries = replay.load_deliveries(data_set, DATA)
    shift_to = None
    if end_at is not None:      # the hour moved so that it ends at the start of the period that holds end_at
        offset = floor(end_at, parse_period(os.environ.get("PERIOD") or "5m")) - SIM_END
        shift_to = min(utc(d["received_time"]) for d in deliveries) + offset
    from page import settings
    counter, ddb, table = Counter(settings()["period"]), sess.client("dynamodb"), table_name()
    totals = {"count_items": 0, "latest_written": 0, "latest_kept": 0}

    def target(batch):
        for k, v in write(counter, counter.add(batch), ddb, table).items():
            totals[k] += v

    run_id = run_id or f"page-{name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    summary = replay.replay(deliveries, replay.LocalSender(target, "load_counts"), run_id, 0.0,
                            evidence=RECIPE / "evidence" / run_id, shift_to=shift_to)
    result = {"data_set": name, "requests": len(counter.seen), "count_items": len(counter.cells),
              "latest_items": len(counter.latest), "sent": summary["sent"], "failed": summary["failed"]}
    log(json.dumps(result))
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("data_set", choices=list(SETS))
    p.add_argument("--end-at", help="'now' or a UTC time: move the hour so that it ends at that period's start")
    a = p.parse_args(argv)
    end_at = None if not a.end_at else datetime.now(timezone.utc) if a.end_at == "now" else utc(a.end_at)
    if os.environ.get("DRY"):
        print(f"(DRY=1) would create {table_name()} if missing and count {a.data_set} into it"
              + (f", the hour ending at {stamp(floor(end_at, timedelta(minutes=5)))}" if end_at else ""))
        return 0
    import boto3
    sess = boto3.session.Session(region_name=REGION)
    ensure_table(sess)
    out = load(a.data_set, end_at, sess)
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
