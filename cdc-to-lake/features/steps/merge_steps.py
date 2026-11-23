"""Steps of features/scheduled_merge.feature (ext-scheduled-merge). Offline: moto for S3 and DynamoDB, DuckDB for
the MERGE statements (merge_run.DuckEngine), each object's LastModified set by the step that puts it."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from behave import given, then, when

TODAY = "2026-11-23"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def at(day: str, hhmm: str) -> datetime:
    return utc(f"{day}T{hhmm}:00Z")


class World:
    """The bucket, the run log and the DuckDB tables of one scenario."""

    def __init__(self):
        import boto3
        import lake
        import merge_run
        import run_log

        self.m = merge_run
        lake.up(log=lambda *_: None)
        run_log.ensure_run_log()
        sess = boto3.session.Session(region_name="ca-central-1")
        self.s3 = sess.client("s3")
        self.bucket = lake.bucket_name(sess)
        self.store = merge_run.Store(self.s3, self.bucket)
        self.table = sess.resource("dynamodb").Table(run_log.run_log_name())
        self.engine = merge_run.DuckEngine()
        self.items: list[dict] = []
        self.n = 0
        self.window_end: datetime | None = None
        self.marked: dict[str, object] = {}

    def run(self, now: datetime) -> dict:
        item = self.m.run(now, store=self.store, table=self.table, engine=self.engine, cfg=self.m.Config(),
                          log=lambda *_: None)
        self.items.append(item)
        return item

    def put(self, deliveries: list[dict], stamp: datetime, day: str | None = None, key: str | None = None,
            raw_lines: list[str] | None = None) -> str:
        self.n += 1
        day = day or stamp.date().isoformat()
        key = key or f"raw/trip-events/dt={day}/{stamp:%Y%m%dT%H%M%S}-{self.n:05d}.jsonl"
        lines = raw_lines if raw_lines is not None else [json.dumps(d, sort_keys=True) for d in deliveries]
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=("\n".join(lines) + "\n").encode())
        self.store.stamps[key] = stamp
        return key

    def trips(self) -> dict[str, dict]:
        cols = [c[0] for c in self.engine.con.execute("SELECT * FROM trips").description]
        return {r[0]: dict(zip(cols, r)) for r in self.engine.rows("SELECT * FROM trips ORDER BY trip_id")}

    def counts(self, where: str = "") -> list[tuple]:
        return self.engine.rows(f"SELECT period_start, pickup_zone_id, trips_requested, trips_completed "
                                f"FROM zone_counts {where} ORDER BY 1, 2")

    def manifest_keys(self) -> dict[str, str]:
        out = {}
        for key, _ in self.store.list("lake/merge-state/"):
            day = key.split("dt=")[1][:10]
            out.update(self.m.manifest(self.store, day))
        return out


def world(context) -> World:
    if not getattr(context, "merge", None):
        context.merge = World()
    return context.merge


def delivery(kind: str, event_id: str, trip: str, event_time: str, received: str | None = None, **fields) -> dict:
    """A delivery line the model accepts: the fields a scenario gives, the rest filled with plain values."""
    d = {"event_type": kind, "event_id": event_id, "trip_id": trip, "event_time": event_time,
         "delivery_id": f"dl-{event_id}", "received_time": received or event_time}
    if kind == "trip_requested":
        d["pickup_zone_id"] = int(fields.get("pickup_zone_id") or 161)
    else:
        d["driver_id"] = fields.get("driver_id") or "d-0001"
    if kind == "trip_completed":
        d.update(dropoff_zone_id=100, trip_miles=1.0, trip_time_s=600, driver_pay=10.0,
                 fare=float(fields.get("fare") or 10.0))
    return d


def from_rows(table) -> list[dict]:
    return [delivery(r["event_type"], r["event_id"], r["trip_id"], r["event_time"],
                     pickup_zone_id=r.get("pickup_zone_id"), driver_id=r.get("driver_id"), fare=r.get("fare"))
            for r in (row.as_dict() for row in table)]


RANK = {"requested": 1, "accepted": 2, "completed": 3}


def seed_trip(w: World, trip: str, state: str, zone=None, requested=None, driver=None, fare=None, last=None):
    """A row as an earlier run left it; its request id and last event id are made up (ev-seed-...)."""
    requested_t = utc(requested).replace(tzinfo=None) if requested else None
    last_t = utc(last).replace(tzinfo=None) if last else requested_t
    w.engine.con.execute(
        "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [trip, state, int(zone) if zone else None, requested_t, f"ev-seed-r-{trip}" if zone else None,
         driver or None, float(fare) if fare else None, last_t, RANK[state], f"ev-seed-{trip}"])


def seed_count(w: World, day: str, zone: int, requested: int, completed: int = 0):
    w.engine.con.execute("INSERT INTO zone_counts VALUES (?, ?, ?, ?, ?)",
                         [date.fromisoformat(day), zone, requested, completed, 0.0])


def window(w: World, start: str, end: str, day: str = TODAY) -> tuple[datetime, datetime]:
    """Objects of the window are stamped in its middle; the window's merge runs at its end."""
    begin, w.window_end = at(day, start), at(day, end)
    return begin + (w.window_end - begin) / 2, w.window_end


def replay_to_s3(w: World, deliveries: list[dict]) -> None:
    """The replayer's s3 target (sim/replay.py), one object a minute; each lands at its slot's end."""
    import replay
    import sim_data

    sender = replay.S3Sender(w.s3, w.bucket, "sim")
    replay.replay(deliveries, sender, "sim")
    for key in sender.keys:
        w.store.stamps[key] = sim_data.slot_end(key)


def hourly(w: World, first: datetime, last: datetime) -> None:
    t = first
    while t <= last:
        w.run(t)
        t += timedelta(hours=1)


# ---- R80 ---------------------------------------------------------------------------------------------------------

@given("these deliveries arrive in the slot {start} to {end}:")
def step_slot(context, start, end):
    w = world(context)
    context.slot = [dict(r.as_dict(), pickup_zone_id=int(r["pickup_zone_id"])) for r in context.table]
    context.slot_start = utc(start + ":00Z")


@when("the slot is written by the replayer's s3 target")
def step_slot_written(context):
    import replay

    w = world(context)
    sender = replay.S3Sender(w.s3, w.bucket, "sim")
    context.summary = replay.replay(context.slot, sender, "sim")
    context.sender = sender


@then("one object under {prefix} for the slot {hhmm} holds delivery {delivery_id}")
def step_slot_object(context, prefix, hhmm, delivery_id):
    w = world(context)
    keys = [k for k, _ in w.store.list(prefix)]
    assert len(keys) == 1, keys
    assert f"{context.slot_start:%Y%m%dT}{hhmm.replace(':', '')}Z" in keys[0], keys[0]
    lines = [json.loads(x) for x in w.store.read(keys[0]).splitlines()]
    assert [d["delivery_id"] for d in lines] == [f"sim-{delivery_id}"], lines


@then("the replayer sent it less than {seconds:d} seconds after the slot was due")
def step_slot_late(context, seconds):
    assert context.summary["sent"] == 1 and context.summary["late_s"] < seconds, context.summary


# ---- R81 ---------------------------------------------------------------------------------------------------------

@given("these trip events arrived in the window {start} to {end}:")
@given("this event arrived in the window {start} to {end}:")
def step_window_events(context, start, end):
    w = world(context)
    stamp, _ = window(w, start, end)
    w.put(from_rows(context.table), stamp)


@when("the merge of that window runs")
@when("the merge of its window runs")
@when("the merge of the window that holds it runs")
def step_merge_window(context):
    w = world(context)
    w.run(w.window_end)


@then('table trips has 1 row for trip {trip}: state "{state}", zone {zone:d}, driver "{driver}", fare {fare:f}')
def step_trip_row(context, trip, state, zone, driver, fare):
    rows = world(context).trips()
    assert list(rows) == [trip], rows
    r = rows[trip]
    assert (r["trip_state"], r["pickup_zone_id"], r["driver_id"], r["fare"]) == (state, zone, driver, fare), r


@given('the simulated data set {data_set} "{name}" has arrived, its late deliveries included')
@given('the simulated data set {data_set} "{name}" has arrived')
def step_data_set(context, data_set, name):
    import sim_data

    assert sim_data.NAMES[data_set] == name
    w = world(context)
    deliveries, context.truth = sim_data.load(data_set)
    replay_to_s3(w, deliveries)


@when("the merges of its windows have run")
def step_merges_ran(context):
    w = world(context)
    hourly(w, at(TODAY, "10:00"), at(TODAY, "13:00"))
    assert w.manifest_keys() and all(v == "merged" for v in w.manifest_keys().values())
    assert len(w.manifest_keys()) == len(w.store.list("raw/trip-events/")), "an object was not merged"


@then("table trips has {n:d} rows, of which {a:d}, {b:d} and {c:d} have zones 48, 161 and 230")
def step_trips_by_zone(context, n, a, b, c):
    w = world(context)
    assert w.engine.rows("SELECT count(*) FROM trips") == [(n,)]
    assert w.engine.rows("SELECT pickup_zone_id, count(*) FROM trips GROUP BY 1 ORDER BY 1") == \
        [(48, a), (161, b), (230, c)]
    truth = context.truth["requested_per_zone"]
    assert [truth["48"], truth["161"], truth["230"]] == [a, b, c], truth


@given("MAX_OBJECTS is {n:d} and {count:d} objects under raw/trip-events/ are in no merge manifest")
def step_backlog(context, n, count):
    os.environ["MAX_OBJECTS"] = str(n)
    w = world(context)
    start = at(TODAY, "08:00")
    for i in range(count):
        t = start + timedelta(seconds=i)
        w.put([delivery("trip_requested", f"ev-b{i:05d}", f"t-b{i:05d}", (t - timedelta(seconds=1))
                        .strftime("%Y-%m-%dT%H:%M:%S.000Z"))], t)


@given("MAX_OBJECTS is {n:d} and {count:d} objects with LastModified {stamp} are in no merge manifest")
def step_same_stamp(context, n, count, stamp):
    os.environ["MAX_OBJECTS"] = str(n)
    w = world(context)
    t = utc(stamp)
    for i in range(count):
        w.put([delivery("trip_requested", f"ev-s{i:05d}", f"t-s{i:05d}", "2026-11-23T09:29:00.000Z")], t,
              key=f"raw/trip-events/dt={TODAY}/same-{i:05d}.jsonl")


@when("the schedule fires three times")
def step_fires_three(context):
    hourly(world(context), at(TODAY, "11:00"), at(TODAY, "13:00"))


@when("the schedule fires twice")
def step_fires_twice(context):
    hourly(world(context), at(TODAY, "11:00"), at(TODAY, "12:00"))


@then("the three runs merge {a:d}, {b:d} and {c:d} objects, oldest first")
def step_three_runs(context, a, b, c):
    items = world(context).items
    assert [i["objects"] for i in items] == [a, b, c], [i["objects"] for i in items]
    assert items[0]["window_end"] < items[1]["window_start"] and items[1]["window_end"] < items[2]["window_start"]


@then("the two runs merge {a:d} and {b:d} objects, in key order")
def step_two_runs(context, a, b):
    items = world(context).items
    assert [i["objects"] for i in items] == [a, b]
    taken = items[0]["taken"] + items[1]["taken"]
    assert taken == sorted(taken), "not in key order"


@then("no object is merged by both runs")
def step_no_twice(context):
    items = world(context).items
    assert not set(items[0]["taken"]) & set(items[1]["taken"])


@given("the run of 10:00 listed raw/trip-events/dt=2026-11-23/ and finished both steps")
def step_run_ten_listed(context):
    w = world(context)
    for i in range(5):
        t = at(TODAY, "09:10") + timedelta(minutes=10 * i)
        w.put([delivery("trip_requested", f"ev-v{i}", f"t-v{i}", (t - timedelta(minutes=1))
                        .strftime("%Y-%m-%dT%H:%M:%S.000Z"))], t if t <= at(TODAY, "09:55") else at(TODAY, "09:55"))
    item = w.run(at(TODAY, "10:00"))
    assert item["run_state"] == "finished" and item["objects"] == 5, item


@given("an object with LastModified {stamp} becomes visible after that listing")
def step_late_visible(context, stamp):
    w = world(context)
    w.marked["late"] = w.put([delivery("trip_requested", "ev-late", "t-late", "2026-11-23T09:19:00.000Z")],
                             utc(stamp))


@when("the run of 11:00 runs")
@when("the run of 11:00 starts")
@when("the run of 11:00 merges those 60 objects again")
def step_run_eleven(context):
    w = world(context)
    w.engine.fail.clear()
    w.run(at(TODAY, "11:00"))


@then("that object is one of the run of 11:00's objects")
def step_late_taken(context):
    assert context.merge.marked["late"] in context.merge.items[-1]["taken"]


@then("none of the run of 10:00's objects is taken again")
def step_none_again(context):
    w = context.merge
    assert not set(w.items[0]["taken"]) & set(w.items[1]["taken"])
    assert w.items[1]["objects"] == 1


# ---- R82 ---------------------------------------------------------------------------------------------------------

@given("table trips has this row:")
def step_seed_row(context):
    w = world(context)
    for r in context.table:
        r = r.as_dict()
        seed_trip(w, r["trip_id"], r["trip_state"], r.get("pickup_zone_id"), r.get("requested_time") or
                  ("2026-11-23T09:00:00Z" if r.get("pickup_zone_id") else None), r.get("driver_id"), r.get("fare"),
                  r["last_event_time"])


@then('the row of {trip} has state "{state}", zone {zone:d}, driver "{driver}" and fare {fare:f}')
def step_row_four(context, trip, state, zone, driver, fare):
    r = world(context).trips()[trip]
    assert (r["trip_state"], r["pickup_zone_id"], r["driver_id"], r["fare"]) == (state, zone, driver, fare), r


@then("the row of {trip} has fare {fare:f}")
def step_row_fare(context, trip, fare):
    assert world(context).trips()[trip]["fare"] == fare


@then('the row of {trip} has state "{state}", zone {zone:d} and requested_time {t}')
def step_row_request(context, trip, state, zone, t):
    r = world(context).trips()[trip]
    assert (r["trip_state"], r["pickup_zone_id"], r["requested_time"]) == \
        (state, zone, utc(t).replace(tzinfo=None)), r


# ---- R90, R91 ---------------------------------------------------------------------------------------------------

@given('the simulated data set SIM02 "late-day" was merged on {day}')
def step_late_day(context, day):
    import sim_data

    w = world(context)
    deliveries, context.truth = sim_data.load("SIM02")
    replay_to_s3(w, deliveries)
    hourly(w, at(day, "10:00"), at(day, "13:00"))


@given("the counts of {day} are {a:d}, {b:d} and {c:d} for zones 48, 161 and 230")
def step_given_counts(context, day, a, b, c):
    w = world(context)
    if day == "2026-11-22" and w.items:          # SIM02: the counts as the merges of 2026-11-22 left them
        return step_counts(context, day, a, b, c)
    for zone, n in zip((48, 161, 230), (a, b, c)):
        seed_count(w, day, zone, n)


@then("the counts of {day} are {a:d}, {b:d} and {c:d} for zones 48, 161 and 230")
def step_counts(context, day, a, b, c):
    rows = world(context).counts(f"WHERE period_start = DATE '{day}'")
    assert [(r[1], r[2]) for r in rows] == [(48, a), (161, b), (230, c)], rows


@then("the counts of {day} are still {a:d}, {b:d} and {c:d}")
def step_counts_still(context, day, a, b, c):
    step_counts(context, day, a, b, c)


@when("the window with its 12 late requests is merged on {day}")
def step_late_window(context, day):
    w = world(context)
    item = w.run(at(day, "17:00"))
    lines = [json.loads(x) for k in item["taken"] for x in w.store.read(k).splitlines()]
    assert item["run_state"] == "finished" and len(lines) == 12, item


@then("the rebuild was one statement")
def step_one_statement(context):
    rebuilds = [sql for step, sql in world(context).engine.started if step == "rebuild"]
    assert rebuilds and all(sql.count("MERGE INTO") == 1 for sql in rebuilds)


@given("table trips has trip {trip} of zone {zone:d} requested at {t}, accepted and not completed")
def step_seed_accepted(context, trip, zone, t):
    seed_trip(world(context), trip, "accepted", zone, t, "d-0006", None, t)


@given("the counts of {day} for zone {zone:d} are {r:d} requested and {c:d} completed")
def step_seed_zone(context, day, zone, r, c):
    seed_count(world(context), day, zone, r, c)


@given("its completion with event_time {t} arrived in the window {start} to {end} on {day}")
def step_completion(context, t, start, end, day):
    w = world(context)
    stamp, _ = window(w, start, end, day)
    w.put([delivery("trip_completed", "ev-000040", "t-0006", t, driver_id="d-0006", fare="15.00")], stamp)


@then("the counts of {day} for zone {zone:d} are {r:d} requested and {c:d} completed")
def step_zone_counts(context, day, zone, r, c):
    rows = world(context).counts(f"WHERE period_start = DATE '{day}' AND pickup_zone_id = {zone}")
    assert [(x[2], x[3]) for x in rows] == [(r, c)], rows


@given("table trips has trip {trip} of zone {zone:d} requested at {t}")
def step_seed_requested(context, trip, zone, t):
    seed_trip(world(context), trip, "requested", zone, t)


@given("the counts of zone {zone:d} on {day} are {n:d} and of zone {other:d} on {other_day} are 0")
def step_seed_moved(context, zone, day, n, other, other_day):
    seed_count(world(context), day, zone, n)


@given("a corrected request of {trip} for zone {zone:d} with event_time {t} arrived on {day}")
def step_corrected_request(context, trip, zone, t, day):
    w = world(context)
    stamp = utc(t) + timedelta(minutes=1)
    w.window_end = stamp + timedelta(hours=1)
    w.put([delivery("trip_requested", "ev-000050", trip, t, pickup_zone_id=zone)], stamp, day=day)


@then("zone_counts has no row of zone {zone:d} on {day}")
def step_no_row(context, zone, day):
    assert world(context).counts(f"WHERE period_start = DATE '{day}' AND pickup_zone_id = {zone}") == []


@then("the counts of zone {zone:d} on {day} are {n:d}")
def step_zone_day(context, zone, day, n):
    rows = world(context).counts(f"WHERE period_start = DATE '{day}' AND pickup_zone_id = {zone}")
    assert [x[2] for x in rows] == [n], rows


@then("the counts of {day} are {a:d}, {b:d} and {c:d} requested and {done:d} completed, as the answer from _truth/ holds")
def step_all_faults(context, day, a, b, c, done):
    step_counts(context, day, a, b, c)
    rows = world(context).counts(f"WHERE period_start = DATE '{day}'")
    assert sum(r[3] for r in rows) == done
    t = context.truth
    assert [t["requested_per_zone_day"][day][z] for z in ("48", "161", "230")] == [a, b, c]
    assert t["completed"] == done
    fares = {k: v["fare"] for k, v in world(context).trips().items() if v["fare"] is not None}
    assert {k.removeprefix("sim-"): v for k, v in fares.items()} == t["fare_by_trip"]


@given('PERIOD is "{period}"')
def step_period(context, period):
    os.environ["PERIOD"] = period


@given("requests of zone {zone:d} on {a} and on {b} have been merged")
def step_two_days(context, zone, a, b):
    w = world(context)
    w.put([delivery("trip_requested", "ev-p1", "t-p1", f"{a}T12:00:00.000Z", pickup_zone_id=zone)],
          at(a, "12:01"), day=a)
    w.put([delivery("trip_requested", "ev-p2", "t-p2", f"{b}T08:00:00.000Z", pickup_zone_id=zone)],
          at(b, "08:01"), day=b)
    w.run(at(b, "10:00"))


@when("the counts of zone {zone:d} are read")
def step_read_counts(context, zone):
    context.read = world(context).counts(f"WHERE pickup_zone_id = {zone}")


@then("they are {rows}")
def step_they_are(context, rows):
    want = [(date.fromisoformat(part.split(" on ")[1]), int(part.split(" on ")[0])) for part in rows.split(" and ")]
    assert [(r[0], r[2]) for r in context.read] == want, context.read


@given("{n:d} requests of zone {zone:d} with event times on {old} arrived in the window {start} to {end} on {day}")
def step_old_requests(context, n, zone, old, start, end, day):
    w = world(context)
    stamp, _ = window(w, start, end, day)
    w.put([delivery("trip_requested", f"ev-o{i}", f"t-o{i}", f"{old}T12:0{i}:00.000Z", pickup_zone_id=zone)
           for i in range(n)], stamp)


@then("table trips has the {n:d} trips")
def step_trips_n(context, n):
    assert len(world(context).trips()) == n


@then("the run's item in the run log has old_events {n:d}")
def step_old_events(context, n):
    w = world(context)
    assert w.m.RunLog(w.table).get(w.items[-1]["run_id"])["old_events"] == n


# ---- R100, R101, R102, R103 ---------------------------------------------------------------------------------------

def sixty(w: World) -> list[str]:
    """60 objects of SIM01, one per minute slot of 09:00 to 09:59, each settled by 10:00."""
    import sim_data

    deliveries, _ = sim_data.load("SIM01")
    slots: dict[str, list] = {}
    for d in deliveries:
        slots.setdefault(d["received_time"][:16], []).append(d)
    keys = []
    for i, (minute, ds) in enumerate(sorted(slots.items())[:60]):
        keys.append(w.put(ds, at(TODAY, "09:00") + timedelta(seconds=50 * i)))
    return keys


@given("the run of 10:00 merged its 60 objects and its rebuild failed")
def step_rebuild_failed(context):
    w = world(context)
    w.marked["sixty"] = sixty(w)
    w.engine.fail["rebuild"] = "put in by the scenario"
    item = w.run(at(TODAY, "10:00"))
    assert item["run_state"] == "failed" and item["objects"] == 60, item
    w.marked["after_ten"] = w.trips()


@then("the rows of trips they touch are the same as after the run of 10:00")
def step_same_rows(context):
    w = world(context)
    assert w.items[-1]["run_state"] == "finished" and w.items[-1]["objects"] == 60
    assert w.trips() == w.marked["after_ten"]


@then("zone_counts equals a count of trips by zone and day")
def step_counts_equal(context):
    w = world(context)
    counted = w.engine.rows("SELECT CAST(date_trunc('day', requested_time) AS DATE), pickup_zone_id, count(*), "
                            "sum(CASE WHEN trip_state = 'completed' THEN 1 ELSE 0 END) FROM trips "
                            "WHERE pickup_zone_id IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2")
    assert w.counts() == counted, (w.counts(), counted)


@given("the run of 10:00 took 60 objects")
def step_took_sixty(context):
    world(context).marked["sixty"] = sixty(world(context))


@given("in that run the {step} fails")
def step_fails(context, step):
    w = world(context)
    w.engine.fail[step] = f"{step} refused (put in by the scenario)"
    w.run(at(TODAY, "10:00"))


@then("its objects include those 60 objects")
def step_include_sixty(context):
    w = world(context)
    assert set(w.marked["sixty"]) <= set(w.items[-1]["taken"]) and w.items[-1]["run_state"] == "finished"


@then('the run log has the run of 10:00 with run_state "{state}", failed_step "{step}" and its error_text')
def step_failed_item(context, state, step):
    w = world(context)
    item = w.m.RunLog(w.table).get(f"{TODAY}T10:00:00.000Z")
    assert (item["run_state"], item["failed_step"]) == (state, step), item
    assert "put in by the scenario" in item["error_text"]


@given("the run of 10:00 recorded a merge query that is still RUNNING")
def step_still_running(context):
    w = world(context)
    sixty(w)
    log = w.m.RunLog(w.table)
    log.put(f"{TODAY}T10:00:00.000Z", run_state="running", merge_query_id="q-still-running",
            started_at=f"{TODAY}T10:00:01.000Z")
    log.put("lock", lock_until=f"{TODAY}T10:15:00.000Z", holder=f"{TODAY}T10:00:00.000Z")
    w.engine.states["q-still-running"] = "RUNNING"


@when("the schedule fires at {hhmm}")
def step_fires_at(context, hhmm):
    world(context).run(at(TODAY, hhmm))


@then('the run of {hhmm} is recorded as "{state}"')
def step_recorded_as(context, hhmm, state):
    w = world(context)
    assert w.m.RunLog(w.table).get(f"{TODAY}T{hhmm}:00.000Z")["run_state"] == state


@then("it starts no query")
def step_no_query(context):
    assert world(context).engine.started == []


@given("an object under raw/trip-events/dt={day}/ holds {n:d} lines, one of them without trip_id")
def step_bad_object(context, day, n):
    w = world(context)
    stamp, _ = window(w, "09:00", "10:00", day)
    lines = []
    for i in range(n):
        d = delivery("trip_requested", f"ev-q{i:03d}", f"t-q{i:03d}", f"{day}T09:{i % 60:02d}:00.000Z")
        if i == 7:
            del d["trip_id"]
        lines.append(json.dumps(d, sort_keys=True))
    w.marked["bad"] = w.put([], stamp, raw_lines=lines)
    w.marked["good"] = w.put([delivery("trip_requested", "ev-q999", "t-q999", f"{day}T09:30:00.000Z")], stamp)


@then("that object is not one of the run's merged objects")
def step_not_merged(context):
    w = world(context)
    states = w.manifest_keys()
    assert states[w.marked["bad"]] == "quarantined" and states[w.marked["good"]] == "merged", states
    assert "t-q000" not in w.trips() and "t-q999" in w.trips()


@then("a copy of it is under {prefix}")
def step_quarantine_copy(context, prefix):
    w = world(context)
    keys = [k for k, _ in w.store.list(prefix)]
    assert [k.rsplit("/", 1)[1] for k in keys] == [w.marked["bad"].rsplit("/", 1)[1]], keys


@then("the run's item in the run log has quarantined {q:d} and bad_lines {b:d}")
def step_quarantine_item(context, q, b):
    w = world(context)
    item = w.m.RunLog(w.table).get(w.items[-1]["run_id"])
    assert (item["quarantined"], item["bad_lines"]) == (q, b), item
    assert "field trip_id" in item["bad_line_list"][0]


# ---- R104: pause and resume

def _schedule(context):
    import boto3
    p = os.environ["PREFIX"]
    return boto3.client("scheduler", region_name="ca-central-1").get_schedule(
        Name=f"{p}-merge-hourly", GroupName=f"{p}-scheduled-merge")


@given("the extension is deployed with merge.py up")
def step_deployed(context):
    import merge
    merge.up(deps=False, log=lambda *_: None, sleep=lambda s: None)
    context.schedule_before = _schedule(context)


@when("merge.py --target aws {command} runs")
def step_pause_resume(context, command):
    import merge
    assert merge.main(["--target", "aws", command]) == 0


@then("the hourly schedule is {state}, with its rate and target unchanged")
def step_schedule_state(context, state):
    s, b = _schedule(context), context.schedule_before
    assert s["State"] == state, s["State"]
    assert (s["ScheduleExpression"], s["Target"]) == (b["ScheduleExpression"], b["Target"])


@then("the merge function is still there")
def step_function_there(context):
    import boto3
    boto3.client("lambda", region_name="ca-central-1").get_function(FunctionName=f"{os.environ['PREFIX']}-merge-run")
