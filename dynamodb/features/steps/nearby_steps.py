"""Steps of features/nearby.feature (ext-nearby). moto stands in for DynamoDB inside this process: nearby.py is
Python and boto3, so the steps call it directly, with made-up credentials and no profile. Every call is logged
through botocore's event hooks, so a Then can count Query, Scan and TransactWriteItems calls."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
from pathlib import Path

from behave import given, then, when

EXT = Path(__file__).resolve().parents[2] / "ext-nearby"
sys.path.insert(0, str(EXT))
sys.path.insert(0, str(EXT / "scripts"))
import clean_up  # noqa: E402
import nearby  # noqa: E402
from tags import ledger_rows  # noqa: E402

FAKE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_REGION": "ca-central-1", "AWS_DEFAULT_REGION": "ca-central-1",
        "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull}
T0 = "2026-11-23T09:10:00Z"
SIX = [("d-101", "standard", 40.7585, -73.9850), ("d-102", "standard", 40.7605, -73.9830),
       ("d-103", "xl", 40.7578, -73.9860), ("d-104", "standard", 40.7550, -73.9890),
       ("d-105", "standard", 40.7630, -73.9800), ("d-106", "standard", 40.7575, -73.9858)]


# ---- the stand-in --------------------------------------------------------------------------------------------------
def _start(context, with_drivers: bool) -> None:
    from moto import mock_aws

    if getattr(context, "nearby_mock", None):
        context.nearby_mock.stop()
    saved = {k: os.environ.get(k) for k in list(FAKE) + ["LEDGER", "MAX_RINGS"]}
    os.environ.update(FAKE)
    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")
    os.environ.pop("MAX_RINGS", None)

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if not getattr(context, "nearby_restore", None):
        context.nearby_restore = True
        context.add_cleanup(restore)
    context.nearby_mock = mock_aws()
    context.nearby_mock.start()
    context.add_cleanup(_stop, context)
    nearby.UNITS.__init__()
    c = nearby.client()
    context.calls = []
    lock = threading.Lock()

    def log(params, model, **kw):
        with lock:
            context.calls.append((model.name, params))

    c.meta.events.register("provide-client-params.dynamodb", log)
    context.c = c
    if with_drivers:
        c.create_table(TableName="recipe-drivers", BillingMode="PAY_PER_REQUEST",
                       KeySchema=[{"AttributeName": "driver_id", "KeyType": "HASH"}],
                       AttributeDefinitions=[{"AttributeName": "driver_id", "AttributeType": "S"}])


def _stop(context):
    if getattr(context, "nearby_mock", None):
        context.nearby_mock.stop()
        context.nearby_mock = None


def calls(context, op: str) -> list[dict]:
    return [p for name, p in context.calls if name == op]


def mark(context) -> None:
    """Forget the calls so far: a Then counts only the calls of the When."""
    context.calls.clear()


def cell_items(context, key: str) -> dict:
    reply = context.c.query(TableName="recipe-drivers-by-cell", KeyConditionExpression="cell_type = :k",
                            ExpressionAttributeValues={":k": {"S": key}}, ConsistentRead=True)
    return {it["driver_id"]["S"]: it for it in reply["Items"]}


def put(context, driver, car, lat, lon, when=T0, **kw):
    context.result = nearby.put_position(context.c, driver, car, float(lat), float(lon), when, **kw)
    return context.result


@given('a local stand-in for DynamoDB with the table "recipe-drivers" of ext-riders-and-drivers')
def step_standin(context):
    _start(context, with_drivers=True)


@given('a local stand-in for DynamoDB without the table "recipe-drivers"')
def step_standin_without(context):
    _start(context, with_drivers=False)


@given("nearby.py has made its tables")
def step_tables(context):
    with contextlib.redirect_stdout(io.StringIO()):
        nearby.tables(context.c)


@when("nearby.py makes its tables again")
def step_tables_again(context):
    mark(context)
    with contextlib.redirect_stdout(io.StringIO()):
        nearby.tables(context.c)


# ---- positions -----------------------------------------------------------------------------------------------------
@given("no position is stored for driver {driver}")
def step_no_position(context, driver):
    assert nearby.read_driver(context.c, driver) is None


@given("driver {driver}, car type {car}, is stored at {lat:g}, {lon:g} in cell {cell} at {when}")
def step_stored_at(context, driver, car, lat, lon, cell, when):
    r = put(context, driver, car, lat, lon, when)
    assert r["result"] == "stored" and r["cell"] == cell, r


@when("driver {driver}, car type {car}, sends the position {lat:g}, {lon:g} at {when}")
def step_sends_with_type(context, driver, car, lat, lon, when):
    mark(context)
    context.sent = (driver, lat, lon, nearby.iso(when))
    put(context, driver, car, lat, lon, when)


@when("driver {driver} sends the position {lat:g}, {lon:g} at {when}")
def step_sends(context, driver, lat, lon, when):
    mark(context)
    put(context, driver, "standard", lat, lon, when)


@when("driver {driver} sends car type {car} and the position {lat:g}, {lon:g} at {when}")
def step_sends_car(context, driver, car, lat, lon, when):
    mark(context)
    put(context, driver, car, lat, lon, when)


@when("the position {lat:g}, {lon:g} of {when} arrives for driver {driver}")
def step_arrives(context, lat, lon, when, driver):
    put(context, driver, "standard", lat, lon, when)


@then('table recipe-drivers-by-cell has driver {driver} under the key "{key}" with that position and time')
def step_has_under(context, driver, key):
    it = cell_items(context, key)[driver]
    _, lat, lon, when = context.sent
    assert (float(it["lat"]["N"]), float(it["lon"]["N"]), it["position_time"]["S"]) == (lat, lon, when), it


@then('"{key}" does not hold driver {driver}')
def step_not_hold(context, key, driver):
    assert driver not in cell_items(context, key)


@then('"{key}" holds driver {driver} at {lat:g}, {lon:g}')
def step_holds_at(context, key, driver, lat, lon):
    it = cell_items(context, key)[driver]
    assert (float(it["lat"]["N"]), float(it["lon"]["N"])) == (lat, lon), it


@then('table recipe-drivers has cell "{cell}" for driver {driver}')
def step_driver_cell(context, cell, driver):
    assert nearby.read_driver(context.c, driver)["cell"] == cell


@then("the three writes were one transaction")
def step_one_tx(context):
    tx = calls(context, "TransactWriteItems")
    assert len(tx) == 1, tx
    kinds = [next(iter(i)) for i in tx[0]["TransactItems"]]
    assert kinds == ["Update", "Put", "Delete"], kinds
    assert not calls(context, "PutItem") and not calls(context, "DeleteItem") and not calls(context, "UpdateItem")


@then('the call reports "{words}"')
def step_reports(context, words):
    assert context.result["result"] == words, context.result


@then('driver {driver} is still stored in "{key}" at {lat:g}, {lon:g} with the time {when}')
def step_still(context, driver, key, lat, lon, when):
    it = cell_items(context, key)[driver]
    assert (float(it["lat"]["N"]), float(it["lon"]["N"])) == (lat, lon)
    d = nearby.read_driver(context.c, driver)
    assert (d["cell"], d["position_time"]) == (key.split("#")[0], nearby.iso(when)), d


@given("writer A and writer B have both read driver {driver} in cell {cell}")
def step_both_read(context, driver, cell):
    context.seen_a = nearby.read_driver(context.c, driver)
    context.seen_b = nearby.read_driver(context.c, driver)
    assert context.seen_a["cell"] == context.seen_b["cell"] == cell


@when("writer A commits the position {lat:g}, {lon:g} of {when}")
def step_writer_a(context, lat, lon, when):
    context.result_a = put(context, "d-101", "standard", lat, lon, when, seen=context.seen_a)
    assert context.result_a["result"] == "stored"


@when("writer B then commits the position {lat:g}, {lon:g} of {when}")
def step_writer_b(context, lat, lon, when):
    context.result_b = put(context, "d-101", "standard", lat, lon, when, seen=context.seen_b)


@then("writer B's first transaction is refused, it reads cell {cell}, and its retry commits")
def step_b_retry(context, cell):
    a = context.result_b["attempts"]
    assert a[0] == {"committed": False, "read_cell": cell} and a[1]["committed"] and len(a) == 2, a


@then('writer B reports "{words}"')
def step_b_reports(context, words):
    assert context.result_b["result"] == words, context.result_b


@then('only "{key}" holds driver {driver}, and "{a}" and "{b}" do not')
def step_only(context, key, driver, a, b):
    assert driver in cell_items(context, key)
    assert driver not in cell_items(context, a) and driver not in cell_items(context, b)


# ---- nearby requests -----------------------------------------------------------------------------------------------
@given("only these drivers are stored:")
def step_only_these(context):
    for row in context.table:
        put(context, row["driver_id"], row["car_type"], row["lat"], row["lon"])


@given("the six drivers of the example above are stored")
def step_six(context):
    for d in SIX:
        put(context, *d)


@given("{n:d} standard drivers are stored within 100 m of {lat:g}, {lon:g}")
def step_n_near(context, n, lat, lon):
    for i in range(n):
        put(context, f"d-2{i:02d}", "standard", lat + 0.0001 * i, lon)


@given('the drivers of SIM02 "drivers-moving" are stored at their start positions')
def step_seed(context):
    with contextlib.redirect_stdout(io.StringIO()):
        nearby.seed(context.c)


@when("a nearby request asks for car type {car} at {lat:g}, {lon:g}")
def step_nearby(context, car, lat, lon):
    mark(context)
    context.answer = nearby.nearby(context.c, lat, lon, car)


@then("the nearby answer is, in this order:")
def step_answer_table(context):
    got = [(d["driver_id"], d["distance_m"]) for d in context.answer["drivers"]]
    want = [(r["driver_id"], int(r["distance_m"])) for r in context.table]
    assert got == want, got


@then("the nearby answer holds {n:d} drivers, nearest first")
def step_answer_n(context, n):
    d = [x["distance_m"] for x in context.answer["drivers"]]
    assert len(d) == n and d == sorted(d), context.answer


@then("the request sent {n:d} Query calls from a pool of {pool:d} threads, for the cells {cells}, and 0 Scan calls")
def step_queries(context, n, pool, cells):
    want = {c.strip() for c in cells.replace(" and ", ", ").split(",")}
    q = calls(context, "Query")
    got = {p["ExpressionAttributeValues"][":k"]["S"].split("#")[0] for p in q}
    assert len(q) == n and got == want and context.answer["pool"] == pool, (len(q), got)
    assert not calls(context, "Scan")


@then("{driver} was read from the neighbour cell {cell}, not from the point's cell {centre}")
def step_from_cell(context, driver, cell, centre):
    d = next(x for x in context.answer["drivers"] if x["driver_id"] == driver)
    assert d["cell"] == cell and nearby.encode(40.7580, -73.9855, 6) == centre, d


@then('the nearby answer is the error "{error}"')
def step_error(context, error):
    assert context.answer == {"error": error}, context.answer


@then("the request made 0 Query calls")
def step_no_query(context):
    assert not calls(context, "Query")


@then("the nearby answer is an empty list with radius {r:d} m")
def step_empty(context, r):
    assert context.answer["drivers"] == [] and context.answer["radius_m"] == r, context.answer


@when('the 5 requests of SIM03 "edge-of-coverage" {where} the service area are sent')
def step_sim03(context, where):
    pts = nearby.read_csv(nearby.DATA / "SIM03" / "points.csv")
    pts = [p for p in pts if p["where"] == where.split()[0]]
    assert len(pts) == 5
    context.answers = [nearby.nearby(context.c, float(p["lat"]), float(p["lon"]), "standard") for p in pts]
    context.answers += [nearby.nearby(context.c, float(p["lat"]), float(p["lon"]), "xl") for p in pts]


@then('each nearby answer is the error "{error}"')
def step_each_error(context, error):
    assert all(a == {"error": error} for a in context.answers), context.answers


@then("each nearby answer is an empty list with radius {r:d} m")
def step_each_empty(context, r):
    assert all(a["drivers"] == [] and a["radius_m"] == r for a in context.answers), context.answers


# ---- the check (R94) -----------------------------------------------------------------------------------------------
@then("nearby_expected.jsonl holds what nearby.py expect computes from SIM01 and SIM02 alone, {n:d} lines")
def step_expected_file(context, n):
    lines = (nearby.DATA / "SIM01" / "nearby_expected.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == nearby.expect() and len(lines) == n


@when("SIM02's positions and SIM01's 837 requests are replayed in time order, with strongly consistent Queries")
def step_replay(context):
    mark(context)
    context.summary = nearby.replay(context.c, log=None)


@then("{n:d} of the {total:d} answers equal their line in nearby_expected.jsonl")
def step_equal_expected(context, n, total):
    s = context.summary
    assert (s["equal_expected"], s["requests"]) == (n, total), s


@then("{n:d} of the {total:d} answers have the same drivers, in the same order, as a strongly consistent Scan of every stored driver of that car type")
def step_equal_check(context, n, total):
    s = context.summary
    assert (s["equal_check"], s["requests"]) == (n, total), s
    assert all(p.get("ConsistentRead") for p in calls(context, "Scan"))


@then('the replay reported as many positions "older, kept" as the answer from SIM02\'s _truth/ counts, {n:d}')
def step_older(context, n):
    truth = nearby.sim_answer()
    assert context.summary["older_kept"] == truth["older_kept"] == n, (context.summary, truth)


@then("every Scan call was made by the check, and every request made 9 Query calls")
def step_scan_by_check(context):
    s = context.summary
    assert len(calls(context, "Scan")) == s["requests"]           # 60 drivers: one page per check
    assert len(calls(context, "Query")) == 9 * s["requests"]
    assert all(p["ConsistentRead"] for p in calls(context, "Query"))


# ---- the bench (R95) -----------------------------------------------------------------------------------------------
@given("{n:d} response times, of which the {slow:d} slowest are {ms:d} ms and the others {fast:d} ms")
def step_times(context, n, slow, ms, fast):
    context.bench = nearby.bench_summary([fast] * (n - slow) + [ms] * slow)


@given("{n:d} response times of {ms:d} ms and {failed:d} failed requests")
def step_failed(context, n, ms, failed):
    context.bench = nearby.bench_summary([ms] * n, failures=failed)


@then("the bench reports p99 {p99} ms and {verdict}")
def step_p99(context, p99, verdict):
    b = context.bench
    assert str(b["p99_ms"]) in (p99, f"{p99}.0") and b["pass"] == (verdict == "pass"), b


@then("the bench reports {verdict}")
def step_verdict(context, verdict):
    assert context.bench["pass"] == (verdict == "pass"), context.bench


@when("the bench runs SIM01 with a clock that advances 1 ms each time it is read")
def step_bench(context):
    mark(context)
    ticks = iter(range(10**9))
    context.bench = nearby.bench(context.c, clock=lambda: next(ticks) / 1000, sleep=lambda s: None)


@then("the bench sent {n:d} requests one at a time, with {q:d} eventually consistent Query calls and 0 Scan calls")
def step_bench_calls(context, n, q):
    assert context.bench["requests"] == n and context.bench["failures"] == 0, context.bench
    queries = calls(context, "Query")
    assert len(queries) == q and not any(p["ConsistentRead"] for p in queries) and not calls(context, "Scan")


@then("the summary of the bench has its p50, p95 and p99, and the command prints its read units before its verdict")
def step_bench_report(context):
    b = context.bench
    assert all(isinstance(b[k], float) for k in ("p50_ms", "p95_ms", "p99_ms")), b
    assert b["p50_ms"] <= b["p95_ms"] <= b["p99_ms"], b
    # the command line: a units line, then the verdict (the live run of 2026-11-23 printed no units)
    import functools

    saved = nearby.bench, nearby.client, nearby.UNITS
    nearby.bench = functools.partial(saved[0], clock=time_ticks(), sleep=lambda s: None)
    nearby.client, nearby.UNITS = (lambda: context.c), nearby.Units()
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert nearby.main(["bench"]) == 0
    finally:
        nearby.bench, nearby.client, nearby.UNITS = saved
    lines = out.getvalue().rstrip().splitlines()
    assert lines[-2].startswith('{"units"') and lines[-1].startswith("PASS: p50 ") and " p95 " in lines[-1], lines[-2:]


def time_ticks():
    ticks = iter(range(10**9))
    return lambda: next(ticks) / 1000


# ---- static points -------------------------------------------------------------------------------------------------
def _load(context):
    with contextlib.redirect_stdout(io.StringIO()):
        context.loaded = nearby.load(context.c, nearby.read_points(nearby.DATA / "SIM04" / "points.csv"))


@given('the simulated data set SIM04 "weather-stations"')
def step_sim04(context):
    assert len(nearby.read_points(nearby.DATA / "SIM04" / "points.csv")) == 4


@when("nearby.py load stores the points")
def step_load(context):
    _load(context)


@given('the simulated data set SIM04 "weather-stations" is loaded')
def step_loaded(context):
    _load(context)


@given('the simulated data set SIM04 "weather-stations" is loaded and MAX_RINGS is {k:d}')
def step_loaded_rings(context, k):
    _load(context)
    os.environ["MAX_RINGS"] = str(k)


@then("the median distance to the nearest other station is {m:d} m")
def step_median(context, m):
    assert round(context.loaded["median_m"]) == m, context.loaded


@then("the precision is {p:d}, whose cell's shorter side is {side:d} m")
def step_precision(context, p, side):
    assert context.loaded["precision"] == p and round(context.loaded["side_m"]) == side, context.loaded
    reply = context.c.get_item(TableName="recipe-points-by-cell", Key=nearby.SETTINGS_KEY)
    assert reply["Item"]["precision"]["N"] == str(p)


@then('s-cpk, s-lga and s-ewr are stored under "{a}" and s-jfk under "{b}"')
def step_stored_under(context, a, b):
    def under(key):
        r = context.c.query(TableName="recipe-points-by-cell", KeyConditionExpression="cell_type = :k",
                            ExpressionAttributeValues={":k": {"S": key}})
        return {i["point_id"]["S"] for i in r["Items"]}
    assert under(a) == {"s-cpk", "s-lga", "s-ewr"} and under(b) == {"s-jfk"}


@when("a nearest request asks for {n:d} points of type {kind} at {lat:g}, {lon:g}")
def step_nearest(context, n, kind, lat, lon):
    mark(context)
    context.answer = nearby.nearest(context.c, lat, lon, kind, n)


@then("the nearest answer is, in this order:")
def step_nearest_table(context):
    got = [(p["point_id"], p["distance_m"]) for p in context.answer["points"]]
    assert got == [(r["point_id"], int(r["distance_m"])) for r in context.table], got
    assert context.answer["complete"]


@then("the request read ring {k:d} only, with {q:d} Query calls, covering {m:d} m")
def step_ring1(context, k, q, m):
    a = context.answer
    assert (a["rings"], a["queries"], a["covered_m"]) == (k, q, m) and len(calls(context, "Query")) == q, a


@then("the nearest answer ends with {point} at {m:d} m")
def step_ends(context, point, m):
    last = context.answer["points"][-1]
    assert (last["point_id"], last["distance_m"]) == (point, m) and context.answer["complete"], context.answer


@then("the request read rings 1 and {k:d}, with {q:d} Query calls, covering {m:d} m")
def step_rings(context, k, q, m):
    a = context.answer
    assert (a["rings"], a["queries"], a["covered_m"]) == (k, q, m) and len(calls(context, "Query")) == q, a


@then("the nearest answer is s-cpk, s-lga and s-ewr with complete false and covered distance {m:d} m")
def step_incomplete(context, m):
    a = context.answer
    assert [p["point_id"] for p in a["points"]] == ["s-cpk", "s-lga", "s-ewr"], a
    assert a["complete"] is False and a["covered_m"] == m, a


@then("{point}, read at {m:d} m, is not in the nearest answer")
def step_not_in(context, point, m):
    assert point not in [p["point_id"] for p in context.answer["points"]]
    assert {"point_id": point, "distance_m": m} in [{k: r[k] for k in ("point_id", "distance_m")}
                                                    for r in context.answer["read"]]


# ---- tables, ledger, clean-up (R110 to R114) -----------------------------------------------------------------------
@then('the tables "{a}" and "{b}" have partition key "{key}", on-demand capacity and a ceiling of {r:d} read and {w:d} write units a second')
def step_table_shape(context, a, b, key, r, w):
    made = {p["TableName"]: p for name, p in context.calls if name == "CreateTable"}
    for t in (a, b):
        d = context.c.describe_table(TableName=t)["Table"]
        assert d["KeySchema"][0] == {"AttributeName": key, "KeyType": "HASH"}
        assert d["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"
        assert made[t]["OnDemandThroughput"] == {"MaxReadRequestUnits": r, "MaxWriteRequestUnits": w}


@then("both tables have the tags Project=436c, Team=recipe, purpose=recipe and prefix=recipe, and a tag Owner")
def step_tags(context):
    for t in ("recipe-drivers-by-cell", "recipe-points-by-cell"):
        arn = context.c.describe_table(TableName=t)["Table"]["TableArn"]
        tags = {x["Key"]: x["Value"] for x in context.c.list_tags_of_resource(ResourceArn=arn)["Tags"]}
        assert {k: tags.get(k) for k in ("Project", "Team", "purpose", "prefix")} == {
            "Project": "436c", "Team": "recipe", "purpose": "recipe", "prefix": "recipe"} and tags.get("Owner"), tags


@then('the ledger records both tables "created" and "recipe-drivers" "borrowed"')
def step_ledger(context):
    rows = {(r["name"], r["state"]) for r in ledger_rows()}
    assert {("recipe-drivers-by-cell", "created"), ("recipe-points-by-cell", "created"),
            ("recipe-drivers", "borrowed")} <= rows, rows


@then('no table was created the second time, and the ledger records each table "created" once')
def step_kept(context):
    assert not calls(context, "CreateTable")
    created = [r["name"] for r in ledger_rows() if r["state"] == "created"]
    assert sorted(created) == ["recipe-drivers-by-cell", "recipe-points-by-cell"], created


@when('"nearby.py tables" runs')
def step_main_tables(context):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        context.code = nearby.main(["tables"])
    context.err = err.getvalue()


@then('it exits with code 1 and names the command to run first, "{cmd}"')
def step_exit_cmd(context, cmd):
    assert context.code == 1 and cmd in context.err, (context.code, context.err)


@then('the stand-in has no table "{a}"')
def step_no_table(context, a):
    assert nearby.table_status(context.c, a) is None


@given("driver d-1 of ext-riders-and-drivers, status available, has a position stored")
def step_parent_driver(context):
    context.c.put_item(TableName="recipe-drivers", Item={"driver_id": {"S": "d-1"}, "status": {"S": "available"}})
    put(context, "d-1", "standard", 40.7585, -73.9850)


@when('the nearby clean-up "{command}" runs')
def step_clean_up(context, command):
    argv = command.split()[1:]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        context.code = clean_up.main(argv, c=context.c, extra_drivers=["d-1"])
    context.out = out.getvalue()


@then("it exits with code {code:d}")
def step_code(context, code):
    assert context.code == code, (context.code, getattr(context, "out", ""))


@then('the stand-in has neither table "{a}" nor "{b}"')
def step_no_tables(context, a, b):
    assert nearby.table_status(context.c, a) is None and nearby.table_status(context.c, b) is None, context.out


@then("the stand-in still has the table \"recipe-drivers\", with driver d-1 available and without a cell, and no item of a SIM02 driver")
def step_drivers_left(context):
    items = context.c.scan(TableName="recipe-drivers")["Items"]
    assert items == [{"driver_id": {"S": "d-1"}, "status": {"S": "available"}}], items


@then('the ledger records both tables "deleted"')
def step_deleted(context):
    last = {}
    for r in ledger_rows():
        last[r["name"]] = r["state"]
    assert last["recipe-drivers-by-cell"] == last["recipe-points-by-cell"] == "deleted", last


@then('the stand-in still has the tables "{a}" and "{b}"')
def step_still_tables(context, a, b):
    assert nearby.table_status(context.c, a) and nearby.table_status(context.c, b), context.out


@given("the ledger is empty")
def step_empty_ledger(context):
    Path(os.environ["LEDGER"]).write_text("")


@given("the ledger of ext-nearby has a line")
def step_ledger_line(context):
    path = context.tmp / "nearby-ledger.jsonl"
    path.write_text('{"type": "dynamodb-table", "name": "recipe-drivers-by-cell", "state": "created", "prefix": "recipe"}\n')
    context.env["LEDGER"] = str(path)


@given('the ledger also records the table "{table}" as created by another recipe')
def step_other_table(context, table):
    context.c.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                           KeySchema=[{"AttributeName": "job", "KeyType": "HASH"}],
                           AttributeDefinitions=[{"AttributeName": "job", "AttributeType": "S"}])
    from tags import record
    record("dynamodb-table", table, "created", script="another recipe")


@given('the nearby clean-up "{command}" has run')
def step_clean_up_ran(context, command):
    step_clean_up(context, command)
    assert context.code == 0, context.out
    mark(context)


@then('the stand-in still has the table "{table}"')
def step_still_one(context, table):
    assert nearby.table_status(context.c, table), context.out


@then("the second clean-up made no write calls")
def step_no_writes(context):
    writes = {"DeleteTable", "UpdateItem", "DeleteItem", "PutItem", "TransactWriteItems", "BatchWriteItem"}
    made = [name for name, _ in context.calls if name in writes]
    assert not made, made
