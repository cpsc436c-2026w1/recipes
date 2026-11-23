"""Steps of features/read_path.feature (ext-read-path). Offline: moto for DynamoDB, a fake URL, log group, function
configuration and clock (tests/read_path_fakes.py)."""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from behave import given, then, when

import read_path_fakes as F
from harness import RECIPE

import read_path  # noqa: E402  (read_path_fakes put ext-read-path on sys.path)
import writer as writer_mod  # noqa: E402


def _rp(context):
    """moto with the two tables, the fakes, and a results file of the scenario's own."""
    if getattr(context, "rp", None):
        return context.rp
    import boto3
    from moto import mock_aws
    os.environ["PREFIX"] = "recipe"
    os.environ["RESULTS"] = str(context.tmp / "results.duckdb")
    context.moto = mock_aws()
    context.moto.start()
    ddb = boto3.client("dynamodb", region_name="ca-central-1")
    clock, logs = F.Clock(), F.FakeLogs()
    lam = F.FakeLambda({"PREFIX": "recipe"}, 1)
    context.rp = {"ddb": ddb, "clock": clock, "logs": logs, "lam": lam, "fail": set(), "break_at": None,
                  "throttle": 0, "writes": [], "tables": False}
    return context.rp


def _tables(context):
    rp = _rp(context)
    if not rp["tables"]:
        F.tables(rp["ddb"])
        rp["tables"] = True


def _note_writes(client, out: list, journal: list):
    def note(params, model, **_):
        if model.name in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem"):
            out.append((model.name, params))
            if model.name == "BatchWriteItem":
                first = next(iter(params["RequestItems"].values()))[0]
                journal.append(("write", "put" if "PutRequest" in first else "delete"))
    client.meta.events.register("provide-client-params.dynamodb.*", note)


def _one_call_at_a_time(*clients):
    """moto keeps its tables in plain dicts and is not thread-safe: the writer thread's UpdateItem during the read
    path's Scan or BatchWriteItem can stop one of them with "dictionary changed size during iteration". DynamoDB
    itself takes both at once; here the two clients' calls take turns."""
    lock = getattr(_one_call_at_a_time, "lock", None) or threading.Lock()
    _one_call_at_a_time.lock = lock
    for client in clients:
        if getattr(client, "_one_at_a_time", False):
            continue
        call = client._make_api_call

        def locked(operation, params, _call=call):
            with lock:
                return _call(operation, params)
        client._make_api_call, client._one_at_a_time = locked, True


def _run(context, argv):
    rp = _rp(context)
    _tables(context)
    api = F.FakeApi(rp["clock"], rp["logs"], rp["lam"], fail=rp["fail"], break_at=rp["break_at"])
    writer_clock = F.Clock()
    writer_ddb = writer_mod.client()
    if rp["throttle"]:
        rp["throttle_state"] = F.throttle_once(writer_ddb, rp["throttle"])
    _note_writes(rp["ddb"], rp["writes"], rp["lam"].journal)
    _note_writes(writer_ddb, rp["writes"], rp["lam"].journal)
    # the handler behind the fake URL has its own two clients (main/handler.py), which read while the writer writes
    _one_call_at_a_time(rp["ddb"], writer_ddb, api.handler.client, api.handler.ddb.meta.client)
    lines = []
    deps = read_path.Deps(send=api, sess=F.Sess(rp["ddb"], rp["lam"], rp["logs"]), now=rp["clock"].now,
                          sleep=rp["clock"].sleep, start_writer=read_path.thread_writer, log=lines.append,
                          writer_clock=writer_clock.monotonic, writer_sleep=writer_clock.sleep, writer_ddb=writer_ddb)
    before = {p for p in RECIPE.rglob("*") if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts}
    try:
        context.rp_code, context.rp_error = read_path.main(argv, deps), None
    except Exception as err:
        context.rp_code, context.rp_error = None, err
    after = {p for p in RECIPE.rglob("*") if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts}
    context.new_files = sorted(str(p.relative_to(RECIPE)) for p in after - before)
    context.rp_out, context.api = "\n".join(lines), api
    import duckdb
    con = duckdb.connect(os.environ["RESULTS"])
    context.rows = _rows(con, "SELECT * FROM calls ORDER BY sent_at, seq")
    context.writer_rows = _rows(con, "SELECT * FROM writer_runs")
    con.close()


def _rows(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---- the plan

@given("the variants of ext-read-path/variants.toml")
def step_variants(context):
    context.variants = read_path.load_variants(RECIPE / "ext-read-path" / "variants.toml", "https://fake/")


@given("variants.toml names the variants topic and topic-author with the same 50 keys")
def step_team_variants(context):
    keys = [f"topic-{i:02d}" for i in range(50)]
    context.variants = [{"name": "topic", "action": "topic", "keys": keys, "url": "https://fake/",
                         "body": {"key_field": "topic_id"}},
                        {"name": "topic-author", "action": "topic-author", "keys": keys, "url": "https://fake/",
                         "body": {"key_field": "topic_id"}}]


@when("ext-read-path/run.sh plans its calls with seed {seed:d}")
def step_plan(context, seed):
    context.plan = read_path.plan(context.variants, seed)
    context.plan_again = read_path.plan(context.variants, seed)


@then("the plan has 600 calls: 200 get-eventual, 200 get-strong and 200 rider")
def step_plan_600(context):
    assert Counter(c.variant for c in context.plan) == {"get-eventual": 200, "get-strong": 200, "rider": 200}


@then("the plan has 400 calls: 200 topic and 200 topic-author")
def step_plan_400(context):
    assert Counter(c.variant for c in context.plan) == {"topic": 200, "topic-author": 200}


@then("each of the trips t-0000 to t-0049 is read 4 times as get-eventual and 4 times as get-strong")
def step_trips_4(context):
    c = Counter((x.variant, x.key) for x in context.plan if x.action == "get")
    assert set(c.values()) == {4} and len(c) == 100, c
    assert {k for _, k in c} == {f"t-{i:04d}" for i in range(50)}
    assert all(x.body.get("strong") is True for x in context.plan if x.variant == "get-strong")


@then("each of the riders r-00 to r-04 is read 40 times")
def step_riders_40(context):
    assert Counter(x.key for x in context.plan if x.action == "rider") == {f"r-{i:02d}": 40 for i in range(5)}


@then("the same seed gives the same order")
def step_same_order(context):
    assert [(c.variant, c.key) for c in context.plan] == [(c.variant, c.key) for c in context.plan_again]


@given('the simulated data set SIM01 "stored-trips" in the fake tables')
def step_sim01(context):
    _tables(context)


@when("ext-read-path/run.sh runs against the fake URL")
def step_run_plain(context):
    _run(context, [])


@when("ext-read-path/run.sh {args} runs against the fake URL")
@when("ext-read-path/run.sh {args} runs against the fake aws")
def step_run_args(context, args):
    _run(context, args.split())


@then("600 calls are stored, each with its read units, caller time, HTTP status and request id")
def step_600_stored(context):
    assert context.rp_error is None, context.rp_error
    rows = [r for r in context.rows if r["kind"] == "planned"]
    assert len(rows) == 600, len(rows)
    assert all(r["read_units"] is not None and r["caller_ms"] is not None and r["status"] == 200 and r["request_id"]
               for r in rows)


@then("no call was sent before the answer of the call before it")
def step_sequential(context):
    spans = context.api.spans
    assert all(spans[i][0] >= spans[i - 1][1] for i in range(1, len(spans))), "two calls overlapped"


# ---- REPORT lines and classes

@given("calls with these request ids and REPORT lines:")
def step_reports(context):
    context.report_rows = [r.as_dict() for r in context.table]


@when("the calls are stored and joined")
def step_join(context):
    import report_lines
    res = read_path.Results(context.tmp / "results.duckdb")
    now = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
    logs = F.FakeLogs()
    for i, r in enumerate(context.report_rows):
        c = read_path.Call("get-eventual", "get", "t-0042", {}, "https://fake/", read="get")
        c.result = {"status": 200, "caller_ms": 100.0, "request_id": r["request_id"], "reply": {"read_units": 0.5}}
        res.add("run-1", i, c, now)
        init = f"\tInit Duration: {r['init_duration_ms']} ms" if r["init_duration_ms"] else ""
        logs.events.append({"timestamp": int(now.timestamp() * 1000), "message":
                            f"REPORT RequestId: {r['request_id']}\tDuration: {r['duration_ms']} ms\tBilled Duration: "
                            f"{r['billed_duration_ms']} ms\tMemory Size: {r['memory_mb']} MB\tMax Memory Used: 92 MB{init}\t"})
    found = report_lines.fetch(logs, "/aws/lambda/recipe-api", 0, 2 ** 62, res.request_ids("run-1"), sleep=lambda s: None)
    res.join_reports(found, "run-1")
    context.rows = _rows(res.con, "SELECT * FROM calls ORDER BY seq")
    res.close()


@then("results.duckdb has both calls with their duration, billed duration, memory size and init duration")
def step_joined(context):
    got = [(r["request_id"], r["duration_ms"], r["billed_ms"], r["memory_mb"], r["init_ms"]) for r in context.rows]
    want = [(r["request_id"], float(r["duration_ms"]), float(r["billed_duration_ms"]), int(r["memory_mb"]),
             float(r["init_duration_ms"]) if r["init_duration_ms"] else None) for r in context.report_rows]
    assert got == want, got


@then("their billed GB-seconds are {a:g} and {b:g}")
def step_gbs(context, a, b):
    import report_lines
    got = [report_lines.gb_seconds(r["billed_ms"], r["memory_mb"]) for r in context.rows]
    assert got == [a, b], got


@then("call {cold_id} is cold and call {warm_id} is warm")
def step_classes(context, cold_id, warm_id):
    classes = {r["request_id"]: r["class"] for r in context.rows}
    assert (classes[cold_id], classes[warm_id]) == ("cold", "warm"), classes


# ---- the idle round

@given("the last call to the URL was at {hhmmss} and the next gap of the plan is {gap:d} minutes")
def step_idle_state(context, hhmmss, gap):
    rp = _rp(context)
    _tables(context)
    res = read_path.Results(context.tmp / "results.duckdb")
    done = read_path.IDLE_GAPS.index(gap)            # this many after-idle calls are stored already
    last = datetime.fromisoformat(f"2026-11-23T{hhmmss}+00:00")
    for i in range(done):
        c = read_path.Call("get-eventual", "get", "t-0000", {}, "https://fake/", read="get", kind="after idle",
                           gap_min=read_path.IDLE_GAPS[i])
        c.result = {"status": 200, "caller_ms": 100.0, "request_id": f"earlier-{i}", "reply": {"read_units": 0.5}}
        res.add("earlier", i, c, last - timedelta(minutes=30 * (done - i)))
    c = read_path.Call("get-eventual", "get", "t-0001", {}, "https://fake/", read="get")
    c.result = {"status": 200, "caller_ms": 100.0, "request_id": "last", "reply": {"read_units": 0.5}}
    res.add("earlier", 99, c, last)
    res.close()
    rp["clock"].t = last


@given("the REPORT line of the next call will have an Init Duration")
def step_next_cold(context):
    _rp(context)["cold_next"] = True


@when("ext-read-path/run.sh --idle is started at {hhmmss}")
def step_idle(context, hhmmss):
    rp = _rp(context)
    rp["clock"].t = datetime.fromisoformat(f"2026-11-23T{hhmmss}+00:00")
    context.calls_before = len(_rows(__import__("duckdb").connect(os.environ["RESULTS"]), "SELECT * FROM calls"))
    _run(context, ["--idle"])


@then("no call is sent")
def step_no_call(context):
    assert context.api.calls == [] and len(context.rows) == context.calls_before, context.rows


@then('it prints "{text}"')
def step_prints(context, text):
    assert text in context.rp_out, context.rp_out


@then('one get is sent and stored as "after idle" with gap {gap:d} and class {cls}')
def step_idle_sent(context, gap, cls):
    assert len(context.api.calls) == 1 and context.api.calls[0]["action"] == "get"
    row = [r for r in context.rows if r["kind"] == "after idle"][-1]
    assert (row["gap_min"], row["class"]) == (gap, cls), row


@then("the next gap of the plan is {gap:d} minutes")
def step_next_gap(context, gap):
    assert f"the next gap is {gap} minutes" in context.rp_out, context.rp_out


# ---- the fresh-copy round

@given("the fake function's update finishes after {n:d} status reads")
def step_update_reads(context, n):
    _rp(context)["lam"].reads_to_finish = n


@then("READ_PATH_ROUND is set to 1, 2 and 3, each before one get")
def step_rounds(context):
    j = _rp(context)["lam"].journal
    sets = [e[1].get("READ_PATH_ROUND") for e in j if e[0] == "update"]
    assert sets[:3] == ["1", "2", "3"], sets
    calls = [i for i, e in enumerate(j) if e[0] == "call"]
    updates = [i for i, e in enumerate(j) if e[0] == "update"]
    assert len(calls) == 3 and all(updates[k] < calls[k] < updates[k + 1] for k in range(3)), j


@then("each get is sent only after LastUpdateStatus is Successful")
def step_after_success(context):
    j = _rp(context)["lam"].journal
    for i, e in enumerate(j):
        if e[0] == "call":
            assert j[i - 1] == ("status", "Successful"), j[max(0, i - 3):i + 1]


@then('the {n:d} calls are stored as "fresh copy", each cold')
def step_fresh_rows(context, n):
    rows = [r for r in context.rows if r["kind"] == "fresh copy"]
    assert [(r["round"], r["class"]) for r in rows] == [(i, "cold") for i in range(1, n + 1)], rows


@then("READ_PATH_ROUND is removed at the end, and PREFIX is kept")
def step_removed(context):
    assert _rp(context)["lam"].env == {"PREFIX": "recipe"}, _rp(context)["lam"].env


# ---- the busy rider and the writer

def _busy_count(context):
    rp = _rp(context)
    return rp["ddb"].query(TableName="recipe-rider-trips", KeyConditionExpression="rider_id = :r",
                           ExpressionAttributeValues={":r": {"S": "r-busy"}}, Select="COUNT")["Count"]


@then("300 trips of r-busy were written before the first rider call for r-busy and are deleted after the last")
def step_busy_written(context):
    assert context.rp_error is None, context.rp_error
    puts = [p for op, p in _rp(context)["writes"] if op == "BatchWriteItem"
            and "PutRequest" in p["RequestItems"]["recipe-rider-trips"][0]]
    assert sum(len(p["RequestItems"]["recipe-rider-trips"]) for p in puts) == 300
    assert _busy_count(context) == 0
    j = _rp(context)["lam"].journal
    calls = [i for i, e in enumerate(j) if e == ("call", "rider")]
    puts = [i for i, e in enumerate(j) if e == ("write", "put")]
    deletes = [i for i, e in enumerate(j) if e == ("write", "delete")]
    assert max(puts) < min(calls) and min(deletes) > max(calls), j[:3]


@then("200 calls are stored as rider for r-busy")
def step_busy_calls(context):
    rows = [r for r in context.rows if r["read"] == "rider r-busy"]
    assert len(rows) == 200 and all(r["key"] == "r-busy" for r in rows)


@given("the fake DynamoDB throttles {n:d} of the writer's updates once each")
def step_throttle(context, n):
    _rp(context)["throttle"] = n


@then("400 busy-rider calls are stored, 200 with writer off and 200 with writer on")
def step_400(context):
    assert context.rp_error is None, context.rp_error
    c = Counter(r["writer"] for r in context.rows if r["read"] == "rider r-busy")
    assert c == {"off": 200, "on": 200}, c


@then("the writer sent no more than {n:d} updates in any second")
def step_pace(context, n):
    assert context.writer_rows[0]["max_per_second"] <= n, context.writer_rows


@then("the writer reports {t:d} throttled and {r:d} retried writes, and {d:d} updates done")
def step_writer_counts(context, t, r, d):
    w = context.writer_rows[0]
    assert (w["throttled"], w["retried"], w["updates_done"]) == (t, r, d), w


# ---- the summary

def _summary_db(context, rows):
    res = read_path.Results(context.tmp / "results.duckdb")
    now = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
    for i, r in enumerate(rows):
        c = read_path.Call(r["variant"], r.get("action", "get"), r.get("key", "t-0000"), {}, "https://fake/",
                           read=r.get("read", r.get("action", "get")), kind=r.get("kind", "planned"),
                           gap_min=r.get("gap_min"))
        c.result = {"status": r.get("status", 200), "caller_ms": r.get("caller_ms", 100.0), "request_id": f"id-{i}",
                    "reply": {"read_units": r.get("read_units", 0.5)}}
        res.add("run-1", i, c, now + timedelta(seconds=i))
        res.con.execute("UPDATE calls SET class = ?, billed_ms = ?, memory_mb = ?, duration_ms = ?, init_ms = ? "
                        "WHERE request_id = ?", [r.get("class", "warm"), r.get("billed_ms", 25), 128,
                                                 r.get("billed_ms", 25) - 0.3, r.get("init_ms"), f"id-{i}"])
    context.summary_rows = rows
    res.close()


@given("the warm get-eventual calls have these caller times:")
def step_caller_times(context):
    rows = []
    for r in context.table:
        rows += [{"variant": "get-eventual", "caller_ms": float(r["caller_ms"])}] * int(r["calls"])
    _summary_db(context, rows)


@given("the warm get-strong calls have these caller times and read units:")
def step_caller_units(context):
    _summary_db(context, [{"variant": "get-strong", "caller_ms": float(r["caller_ms"]),
                           "read_units": float(r["read_units"])} for r in context.table])


@given("4 after-idle calls with gaps 5, 15, 30 and 60 and caller times 98, 101, 812 and 840 ms")
def step_idle_rows(context):
    rows = [{"variant": "get-eventual", "caller_ms": 100.0}] * 20
    rows += [{"variant": "get-eventual", "kind": "after idle", "gap_min": g, "caller_ms": float(t),
              "class": "cold" if t > 500 else "warm", "init_ms": 480.0 if t > 500 else None}
             for g, t in ((5, 98), (15, 101), (30, 812), (60, 840))]
    _summary_db(context, rows)


@given("the medians of get-eventual are billed 25 ms at 128 MB and 0.5 read units")
def step_med_eventual(context):
    context.cost_rows = [{"variant": "get-eventual", "billed_ms": 25, "read_units": 0.5}] * 3


@given("the medians of get-strong are billed 25 ms at 128 MB and 1.0 read unit")
def step_med_strong(context):
    context.cost_rows += [{"variant": "get-strong", "billed_ms": 25, "read_units": 1.0}] * 3


@given("a cold get-eventual is billed 810 ms at 128 MB and 0.5 read units")
def step_med_cold(context):
    context.cost_rows += [{"variant": "get-eventual", "billed_ms": 810, "read_units": 0.5, "class": "cold",
                           "init_ms": 493.18}]
    _summary_db(context, context.cost_rows)


@given("200 calls of rider, of which 2 answered HTTP 429")
def step_429(context):
    _summary_db(context, [{"variant": "rider", "action": "rider", "read": "rider",
                           "status": 429 if i in (17, 140) else 200} for i in range(200)])


@when("the summary is written")
def step_summary(context):
    import duckdb
    import tomllib
    con = duckdb.connect(str(context.tmp / "results.duckdb"))
    prices = tomllib.loads((RECIPE / "prices.toml").read_text())
    context.summary = read_path.summary(con, prices)
    context.srows = _rows_params(con, (RECIPE / "ext-read-path" / "summary.sql").read_text(),
                                 read_path.price_params(prices))
    con.close()


def _rows_params(con, sql, params):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _srow(context, variant, cls, read=None):
    return next(r for r in context.srows if r["variant"] == variant and r["class"] == cls
                and (read is None or r["read"] == read))


@then("the row get-eventual, warm has 200 calls, p50 {p50:g} ms, p95 {p95:g} ms and p99 {p99:g} ms")
def step_row_200(context, p50, p95, p99):
    r = _srow(context, "get-eventual", "warm")
    got = (r["calls"], round(r["p50_ms"], 3), round(r["p95_ms"], 3), round(r["p99_ms"], 3))
    assert got == (200, p50, p95, p99), got
    assert "| get-eventual | get | warm | off | 200 | 0 | 100.0 | 110.0 | 306.0 |" in context.summary, context.summary


@then("the row get-strong, warm has 10 calls, p50 {p50:g} ms and median read units {units:g}")
def step_row_10(context, p50, units):
    r = _srow(context, "get-strong", "warm")
    assert (r["calls"], r["p50_ms"], r["median_read_units"]) == (10, p50, units), r


@then('its p95 and p99 say "{a}" and "{b}"')
def step_too_few(context, a, b):
    line = next(l for l in context.summary.splitlines() if l.startswith("| get-strong | get | warm"))
    assert f"| {a} | {b} |" in line, line


@then("the summary lists 4 after-idle lines with those gaps and times, below the warm and cold rows")
def step_idle_lines(context):
    lines = context.summary.splitlines()
    idle = [l for l in lines if l.startswith("| after idle")]
    assert [re.findall(r"\| (\d+) min \|", l)[0] for l in idle] == ["5", "15", "30", "60"], idle
    assert [l.split("|")[5].strip() for l in idle] == ["98.0", "101.0", "812.0", "840.0"], idle
    assert lines.index(idle[0]) > max(i for i, l in enumerate(lines) if l.startswith("| get-eventual"))


@then("1,000 calls cost {a} USD for get-eventual, {b} USD for get-strong and {c} USD for a cold get-eventual")
def step_costs(context, a, b, c):
    got = [f"{_srow(context, 'get-eventual', 'warm')['cost_1000_usd']:.6f}",
           f"{_srow(context, 'get-strong', 'warm')['cost_1000_usd']:.6f}",
           f"{_srow(context, 'get-eventual', 'cold')['cost_1000_usd']:.6f}"]
    assert got == [a, b, c], got


@then("the row rider counts 198 calls in its percentiles and 2 failed calls")
def step_failed(context):
    r = _srow(context, "rider", "all")
    assert (r["calls"], r["failed"]) == (198, 2), r


# ---- what a run changes

@given("the fake URL fails one call")
def step_fail_one(context):
    _rp(context)["fail"] = {7}


@given("the fake URL breaks on the 10th busy-rider call")
def step_break(context):
    _rp(context)["break_at"] = 9


@then("the only items written, updated or deleted are the 300 trips of r-busy, and none of them is left")
def step_only_busy(context):
    assert context.rp_error is None, context.rp_error
    for op, p in _rp(context)["writes"]:
        if op == "BatchWriteItem":
            for req in p["RequestItems"]["recipe-rider-trips"]:
                key = (req.get("PutRequest", {}).get("Item") or req["DeleteRequest"]["Key"])
                assert key["rider_id"] == {"S": "r-busy"}, req
        else:
            assert p["TableName"] == "recipe-rider-trips" and p["Key"]["rider_id"] == {"S": "r-busy"}, (op, p)
    assert _busy_count(context) == 0
    assert sum(1 for r in context.rows if r["status"] != 200) == 1


@then("the only change to the function is READ_PATH_ROUND, which is removed at the end")
def step_only_var(context):
    lam = _rp(context)["lam"]
    assert all(set(u) <= {"PREFIX", "READ_PATH_ROUND"} and u["PREFIX"] == "recipe" for u in lam.updates), lam.updates
    assert lam.env == {"PREFIX": "recipe"}


@then("the only files it wrote are results.duckdb and its ledger lines, which end with both undone")
def step_only_files(context):
    from tags import ledger_rows
    assert context.new_files == [], context.new_files          # nothing new in the recipe folder
    written = {p.name for p in context.tmp.iterdir() if p.is_file()}
    assert written <= {"results.duckdb", "results.duckdb.wal", "ledger.jsonl"}, written
    last = {}
    for r in ledger_rows():
        last[(r["type"], r["name"])] = r["state"]
    assert last == {("dynamodb-items", "recipe-rider-trips:r-busy"): "deleted",
                    ("lambda-env", "recipe-api:READ_PATH_ROUND"): "deleted"}, last


@given("rider r-busy already has one trip in the fake tables")
def step_busy_there(context):
    _tables(context)
    _rp(context)["ddb"].put_item(TableName="recipe-rider-trips", Item={
        "rider_id": {"S": "r-busy"}, "requested_at": {"S": "2026-09-30T00:00:00Z"}, "trip_id": {"S": "t-team"}})


@given("a stopped run left one of the 300 trips of r-busy in the fake tables")
def step_busy_left(context):
    _tables(context)
    put = read_path.busy_items("put")[0]["PutRequest"]["Item"]
    _rp(context)["ddb"].put_item(TableName="recipe-rider-trips", Item=put)


@given("the fake function already has READ_PATH_ROUND")
def step_var_there(context):
    _rp(context)["lam"].env["READ_PATH_ROUND"] = "team"


@given("the ledger records both as created by an earlier run")
def step_ledger_created(context):
    from tags import record
    record("dynamodb-items", "recipe-rider-trips:r-busy", "created", script="ext-read-path/run.sh --busy-rider")
    record("lambda-env", "recipe-api:READ_PATH_ROUND", "created", script="ext-read-path/run.sh --fresh")


@then("the run stops with exit code 1 before its first call")
def step_stopped(context):
    assert context.rp_error is None and context.rp_code == 1, (context.rp_code, context.rp_error)
    assert context.api.calls == [] and context.rows == [], context.rows


@then("the trip of r-busy and READ_PATH_ROUND are as they were")
def step_as_they_were(context):
    rp = _rp(context)
    assert _busy_count(context) == 1 and rp["lam"].updates == [], rp["lam"].updates
    assert rp["lam"].env == {"PREFIX": "recipe", "READ_PATH_ROUND": "team"}, rp["lam"].env


@then("the run ends with an error")
def step_error(context):
    assert context.rp_error is not None


@then("none of the 300 trips of r-busy is left")
def step_none_left(context):
    assert _busy_count(context) == 0


# ---- live (excluded by default: behave.ini)

@given("the API of main/run.sh is up with KEEP=1")
@when("ext-read-path/run.sh --busy-rider runs in the sandbox")
@then("the median read units are 3.0 for rider r-busy and 0.5 for rider r-03")
def step_live_rp(context):
    raise AssertionError("a @live example: run it in a sandbox by hand, README 'Run it live'")
