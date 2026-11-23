"""Steps of features/dashboard_page.feature (ext-dashboard-page). Offline: moto, a fake Athena, headless Chromium."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

from behave import given, then, when

from harness import RECIPE

EXT = RECIPE / "ext-dashboard-page"
for p in (str(EXT), str(RECIPE / "scripts"), str(RECIPE / "sim")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _moto(context):
    """moto for this scenario, with the counts table; page.py's client is made again inside it."""
    if getattr(context, "dash", None):
        return context.dash
    import boto3
    from moto import mock_aws
    import page
    os.environ["PREFIX"] = "recipe"
    context.moto = mock_aws()
    context.moto.start()
    page._ddb = None
    sess = boto3.session.Session(region_name="ca-central-1")
    import load_counts
    load_counts.ensure_table(sess, log=lambda *_: None)
    context.dash = {"sess": sess, "ddb": sess.client("dynamodb"), "page": page}
    return context.dash


class FakeAthena:
    """Answers StartQueryExecution / GetQueryExecution / GetQueryResults with the rows a scenario gives."""

    def __init__(self, rows: list[dict] | None = None, fail: str | None = None):
        self.rows, self.fail, self.calls, self.sql = rows or [], fail, 0, []
        self.workgroups = []

    def start_query_execution(self, QueryString, WorkGroup):
        self.calls += 1
        self.sql.append(QueryString)
        self.workgroups.append(WorkGroup)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        self.calls += 1
        if self.fail:
            return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.fail}}}
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, QueryExecutionId, **_):
        self.calls += 1
        head = ["pickup_zone_id", "period_start", "trips_requested", "newest_requested_time"]
        rows = [{"Data": [{"VarCharValue": h} for h in head]}]
        rows += [{"Data": [{"VarCharValue": str(r[h])} for h in head]} for r in self.rows]
        return {"ResultSet": {"Rows": rows}}


def _get(context, path: str, at: str | None = None, method: str = "GET"):
    d = _moto(context)
    path, _, query = path.partition("?")
    qs = dict(q.split("=", 1) for q in query.split("&")) if query else None
    event = {"version": "2.0", "rawPath": path, "queryStringParameters": qs,
             "requestContext": {"http": {"method": method, "path": path}}}
    out = d["page"].handle(event, now=_t(at) if at else _t("2026-11-23T10:00:30Z"))
    context.answer = out
    context.page_calls = list(d["page"].CALLS)
    try:
        context.json = json.loads(out["body"])
    except ValueError:
        context.json = None
    return out


def _zone(context, zone: int) -> dict:
    return next(z for z in context.json["zones"] if z["zone_id"] == zone)


def _value(context, zone: int, label: str):
    periods = context.json["periods"]
    idx = next(i for i, p in enumerate(periods) if p[11:16] == label or p[:10] == label)
    return _zone(context, zone)["values"][idx]


def _put(context, zone, period, **attrs):
    item = {"zone_id": {"N": str(zone)}, "period_start": {"S": period}}
    for k, v in attrs.items():
        item[k] = {"N": str(v)} if isinstance(v, int) else {"S": v}
    _moto(context)["ddb"].put_item(TableName="recipe-zone-counts", Item=item)


def _item(context, zone, period):
    it = _moto(context)["ddb"].get_item(TableName="recipe-zone-counts", ConsistentRead=True,
                                        Key={"zone_id": {"N": str(zone)}, "period_start": {"S": period}}).get("Item")
    return None if it is None else {k: next(iter(v.values())) for k, v in it.items()}


# ---- data

@given('the simulated data set "{name}" has been counted')
def step_counted(context, name):
    import load_counts
    d = _moto(context)
    out = load_counts.load(name, None, d["sess"], log=lambda *_: None, run_id=f"scenario-{name}")
    context.loaded = out


@given("the latest items of zones 161 and 230 are {t161} and {t230}, and zone 48 has none")
def step_latest_override(context, t161, t230):
    ddb = _moto(context)["ddb"]
    ddb.delete_item(TableName="recipe-zone-counts", Key={"zone_id": {"N": "48"}, "period_start": {"S": "latest"}})
    _put(context, 161, "latest", newest_event_time=t161)
    _put(context, 230, "latest", newest_event_time=t230)


@given("only these counts are stored:")
def step_only_counts(context):
    _moto(context)
    for row in context.table:
        _put(context, int(row["zone_id"]), row["period_start"], requested=int(row["requested"]),
             newest_event_time=row["newest_event_time"])


@given("only these latest items are stored:")
def step_only_latest(context):
    _moto(context)
    for row in context.table:
        _put(context, int(row["zone_id"]), "latest", newest_event_time=row["newest_event_time"])


@given("no item is stored")
def step_no_item(context):
    _moto(context)


@given('PERIOD is "{period}" and STALE_MINUTES is "{minutes}"')
def step_period(context, period, minutes):
    os.environ["PERIOD"], os.environ["STALE_MINUTES"] = period, minutes


@given('SLOTS_TABLE is "{table}"')
def step_slots_table(context, table):
    d = _moto(context)
    os.environ["SLOTS_TABLE"] = table
    d["ddb"].create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                          KeySchema=[{"AttributeName": "slot_day", "KeyType": "HASH"},
                                     {"AttributeName": "slot_start", "KeyType": "RANGE"}],
                          AttributeDefinitions=[{"AttributeName": "slot_day", "AttributeType": "S"},
                                                {"AttributeName": "slot_start", "AttributeType": "S"}])


@given("only these slot items are stored for day {day}:")
def step_slot_items(context, day):
    ddb = _moto(context)["ddb"]
    for row in context.table:
        ddb.put_item(TableName=os.environ["SLOTS_TABLE"], Item={
            "slot_day": {"S": day}, "slot_start": {"S": row["slot_start"]}, "slot_state": {"S": row["slot_state"]},
            "record_total": {"N": "200"}})


# ---- reads

@when("a browser sends GET / at {at}")
def step_get_root(context, at):
    _get(context, "/", at)


@when("GET {path} is read at {at}")
def step_get_at(context, path, at):
    _get(context, path, at)


@when("these requests are sent to the page:")
def step_requests(context):
    context.statuses, context.calls_total = [], 0
    for row in context.table:
        out = _get(context, row["path"], method=row["method"])
        context.statuses.append(out["statusCode"])
        context.calls_total += len(context.page_calls)
    context.page_calls = [None] * context.calls_total


@then('the answer is HTTP {status:d} with content type "{ctype}"')
def step_answer_ctype(context, status, ctype):
    assert context.answer["statusCode"] == status, context.answer
    assert context.answer["headers"]["content-type"].startswith(ctype), context.answer["headers"]


@then("the answer is HTTP {status:d}")
def step_answer(context, status):
    assert context.answer["statusCode"] == status, context.answer


@then("the page's script reads /api/zone-counts")
def step_script_reads(context):
    assert re.search(r"fetch\(\s*[\"'`]/api/zone-counts", context.answer["body"]), "no fetch of /api/zone-counts"


@then("the periods start at {first}, {second} and so on to {last}")
def step_periods(context, first, second, last):
    p = context.json["periods"]
    assert len(p) == 12, p

    def short(x):
        return x[:10] if len(first) == 10 else x[11:16]
    assert [short(p[0]), short(p[1]), short(p[-1])] == [first, second, last], p


@then("zone 48 has 5 for 09:20, null for 09:25 and 09:30, and 6 for 09:35")
def step_z48(context):
    assert [_value(context, 48, t) for t in ("09:20", "09:25", "09:30", "09:35")] == [5, None, None, 6]


@then("zone 161 has 4 for 09:25 and null for the other 11 periods, without the period of 10:00")
def step_z161(context):
    values = _zone(context, 161)["values"]
    assert _value(context, 161, "09:25") == 4 and sum(v is None for v in values) == 11, values
    assert not any(p[11:16] == "10:00" for p in context.json["periods"])


@then("zone {zone:d} has null for all 12 periods")
def step_all_null(context, zone):
    assert _zone(context, zone)["values"] == [None] * 12, _zone(context, zone)


@then("every zone has null for all 12 periods")
def step_every_null(context):
    assert all(z["values"] == [None] * 12 for z in context.json["zones"]), context.json["zones"]


@then("zone 48 has null for 09:25 and 09:30")
def step_quiet(context):
    assert _value(context, 48, "09:25") is None and _value(context, 48, "09:30") is None


@then("the values of zones 48, 161 and 230 add up to {a:d}, {b:d} and {c:d}")
def step_sums(context, a, b, c):
    sums = [sum(v or 0 for v in _zone(context, z)["values"]) for z in (48, 161, 230)]
    assert sums == [a, b, c], sums


@then("the JSON has data_as_of {as_of} and read_at {read_at}")
def step_as_of(context, as_of, read_at):
    assert (context.json["data_as_of"], context.json["read_at"]) == (as_of, read_at), context.json


def _check_zone(context, zone, as_of, age, stale):
    z = _zone(context, zone)
    got = z["as_of"][11:19] if len(as_of) == 8 and z["as_of"] else z["as_of"]
    assert (got, z["age_minutes"], z["stale"]) == (as_of, age, stale), z


@then("zone {zone:d} has as_of {as_of}, age {age:d} minutes, not stale")
def step_zone_fresh(context, zone, as_of, age):
    _check_zone(context, zone, as_of, age, False)


@then("zone {zone:d} has as_of null and is stale")
def step_zone_never(context, zone):
    z = _zone(context, zone)
    assert z["as_of"] is None and z["stale"] is True, z


@then("zone {zone:d} is stale with age {age:d} minutes")
def step_zone_stale(context, zone, age):
    z = _zone(context, zone)
    assert z["stale"] is True and z["age_minutes"] == age, z


@then("zone {zone:d} has as_of {as_of} and is stale with age {age:d} minutes")
def step_zone_stale_at(context, zone, as_of, age):
    _check_zone(context, zone, as_of, age, True)


@then("every zone is stale with an age of at least {age:d} minutes")
def step_every_stale(context, age):
    assert all(z["stale"] and z["age_minutes"] >= age for z in context.json["zones"]), context.json["zones"]


@then("the page is not stale")
def step_page_fresh(context):
    assert context.json["stale"] is False, context.json


@then("the page is stale")
def step_page_stale(context):
    assert context.json["stale"] is True, context.json


@then("the JSON has data_as_of {as_of} and the page is stale")
def step_page_stale_at(context, as_of):
    want = None if as_of == "null" else as_of
    assert context.json["data_as_of"] == want and context.json["stale"] is True, context.json


@then('the JSON\'s message is "{message}"')
def step_message(context, message):
    assert context.json["message"] == message, context.json


@then("the answers are {a:d}, {b:d} and {c:d}")
def step_answers(context, a, b, c):
    assert context.statuses == [a, b, c], context.statuses


@then("the page called DynamoDB {n:d} times")
def step_ddb_n(context, n):
    assert len(context.page_calls) == n, context.page_calls


@then("the page called DynamoDB with {n:d} Query calls, each eventually consistent, and 0 Scan calls")
def step_ddb_queries(context, n):
    ops = [op for op, _ in context.page_calls]
    assert ops == ["Query"] * n, ops
    assert all(params["ConsistentRead"] is False for _, params in context.page_calls)


@then("the page called DynamoDB with 1 Query call, strongly consistent, and 0 Scan calls")
def step_ddb_strong(context):
    ops = [op for op, _ in context.page_calls]
    assert ops == ["Query"], ops
    assert context.page_calls[0][1]["ConsistentRead"] is True


# ---- the lake job

def _cfg():
    import refresh
    return refresh.settings()


def _runs_table(context):
    from run_log import ensure_run_log
    d = _moto(context)
    if not d.get("runs"):
        ensure_run_log(d["sess"])
        d["runs"] = True


def _run_job(context, at):
    import refresh
    _runs_table(context)
    d = _moto(context)
    context.athena = getattr(context, "athena", None) or FakeAthena()
    calls_before = context.athena.calls
    try:
        context.job = refresh.run(_t(at), context.athena, d["ddb"], refresh.settings(),
                                  clock=lambda tz=None: _t(at), sleep=lambda s: None)
        context.job_error = None
    except Exception as err:          # recorded in the run log, then raised: the scenario reads the log
        context.job, context.job_error = None, err
    context.athena_calls_this_run = context.athena.calls - calls_before


def _run_item(context, run_id):
    it = _moto(context)["ddb"].get_item(TableName="recipe-runs", ConsistentRead=True,
                                        Key={"job": {"S": "dash-refresh"}, "run_id": {"S": run_id}}).get("Item")
    return None if it is None else {k: next(iter(v.values())) for k, v in it.items()}


@given("the fake Athena answers the summary query with these rows:")
def step_athena_rows(context):
    context.athena = FakeAthena([dict(r.as_dict()) for r in context.table])


@given('the fake Athena fails the summary query with "{reason}"')
def step_athena_fails(context, reason):
    context.athena = FakeAthena(fail=reason)


@given("the schedule has started the lake job at {at}")
@when("the schedule starts the lake job at {at}")
def step_job(context, at):
    _run_job(context, at)


@when("the schedule starts the lake job again at {at}")
def step_job_again(context, at):
    _run_job(context, at)


@given("the lake job has written its items")
def step_job_written(context):
    context.athena = FakeAthena([{"pickup_zone_id": 48, "period_start": "2026-11-22", "trips_requested": 4688,
                                  "newest_requested_time": "2026-11-22 23:59:02.000"}])
    _run_job(context, "2026-11-23T01:00:00Z")
    context.athena_before_page = context.athena.calls


@when("GET / and GET /api/zone-counts are read")
def step_read_both(context):
    """Every AWS client the page makes during the two reads is noted, by service."""
    import boto3
    page = _moto(context)["page"]
    made, real = [], boto3.client

    def noting(service, *a, **kw):
        made.append(service)
        return real(service, *a, **kw)
    page._ddb, boto3.client = None, noting
    try:
        _get(context, "/")
        _get(context, "/api/zone-counts", "2026-11-23T01:05:00Z")
    finally:
        boto3.client = real
    context.clients_made = made


@then("Athena was called 0 times by the page's function")
def step_no_athena_page(context):
    assert context.clients_made == ["dynamodb"], context.clients_made
    assert context.athena.calls == context.athena_before_page


@then("the counts table has {n:d} count items and the latest items 48 at {t48} and 161 at {t161}")
def step_counts_table(context, n, t48, t161):
    items = _moto(context)["ddb"].scan(TableName="recipe-zone-counts")["Items"]
    counts = [i for i in items if i["period_start"]["S"] != "latest"]
    latest = {i["zone_id"]["N"]: i["newest_event_time"]["S"] for i in items if i["period_start"]["S"] == "latest"}
    assert len(counts) == n, counts
    assert latest == {"48": t48, "161": t161}, latest


@then("the run log has job dash-refresh, run {run_id}, run_state {state}")
def step_run_state(context, run_id, state):
    item = _run_item(context, run_id)
    assert item and item["run_state"] == state, item


@then('the job ran its query in the workgroup "{wg}"')
def step_workgroup(context, wg):
    assert context.athena.workgroups == [wg], context.athena.workgroups
    sql = context.athena.sql[0]
    assert "zone_counts" in sql and "trips" in sql and "DATE '2026-11-11'" in sql and "DATE '2026-11-23'" in sql, sql


@then('the run log has run {run_id} with run_state failed, failed_step "{step}" and an error_text with "{text}"')
def step_run_failed(context, run_id, step, text):
    item = _run_item(context, run_id)
    assert item["run_state"] == "failed" and item["failed_step"] == step and text in item["error_text"], item


@then("the run log has no lock held for dash-refresh")
def step_no_lock(context):
    assert _run_item(context, "lock") is None


@given("the latest item of zone 48 is {t}")
def step_latest48(context, t):
    _put(context, 48, "latest", newest_event_time=t)


@given("the count item of zone 48 for {day} has requested {n:d} and generation {g}")
def step_count48(context, day, n, g):
    _put(context, 48, day, requested=n, generation=g)


@given("a run of the lake job that started at {start} finishes now with zone 48's newest time {t} and {n:d} requested for {day}")
def step_late_run(context, start, t, n, day):
    context.late = (start, [{"pickup_zone_id": "48", "period_start": day, "trips_requested": str(n),
                             "newest_requested_time": t}])


@when("it writes the count items and the latest items")
def step_late_write(context):
    import refresh
    start, rows = context.late
    try:
        context.late_out, context.late_error = refresh.write(rows, start, _moto(context)["ddb"], refresh.settings()), None
    except Exception as err:
        context.late_out, context.late_error = None, err


@then("the latest item of zone 48 is still {t}")
def step_latest_still(context, t):
    assert _item(context, 48, "latest")["newest_event_time"] == t


@then("the count item of zone 48 for {day} still has requested {n:d}")
def step_count_still(context, day, n):
    assert int(_item(context, 48, day)["requested"]) == n


@then("the run counts 1 latest item kept, 1 count item kept for zone 48 and 0 errors")
def step_late_counts(context):
    assert context.late_error is None, context.late_error
    out = context.late_out
    assert out["latest_kept"] == 1 and out["kept_by_zone"] == {48: 1} and out["count_written"] == 0, out


@given("the run log has job dash-refresh, run lock, with lock_until {until}")
def step_lock(context, until):
    _runs_table(context)
    _moto(context)["ddb"].put_item(TableName="recipe-runs", Item={
        "job": {"S": "dash-refresh"}, "run_id": {"S": "lock"}, "lock_until": {"S": until}, "holder": {"S": "earlier"}})
    context.athena = FakeAthena()


@then("Athena was called 0 times by that run")
def step_athena_zero(context):
    assert context.athena_calls_this_run == 0


@then("the run log has run {run_id} with run_state skipped")
def step_skipped(context, run_id):
    assert _run_item(context, run_id)["run_state"] == "skipped"


# ---- the public URL

def _deploy(context, argv, at=None):
    import deploy
    d = _moto(context)
    lines = []
    code = deploy.main(argv, sess=d["sess"], now=_t(at) if at else None, log=lines.append, sleep=lambda s: None)
    context.deploy_out, context.deploy_code = lines, code
    return code


@given("the page is deployed against the fake aws")
def step_deployed(context):
    assert _deploy(context, ["up"]) == 0, context.deploy_out


@given("the fake aws refuses CreateSchedule")
def step_refuse_schedule(context):
    from botocore.exceptions import ClientError

    def refuse(**_):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "refused by the test"}},
                          "CreateSchedule")
    _moto(context)["sess"].events.register("before-call.scheduler.CreateSchedule", refuse)


@given("run.sh url-on {hours:d} ran at {at} against the fake aws")
@when("run.sh url-on {hours:d} runs at {at} against the fake aws")
def step_url_on(context, hours, at):
    _deploy(context, ["url-on", str(hours)], at)


@when("run.sh url-off runs against the fake aws")
def step_url_off(context):
    _deploy(context, ["url-off"])


def _url(context):
    lam = _moto(context)["sess"].client("lambda")
    try:
        return lam.get_function_url_config(FunctionName="recipe-page")
    except lam.exceptions.ResourceNotFoundException:
        return None


@then("the page function has a function URL with auth type NONE")
def step_has_url(context):
    assert context.deploy_code == 0, context.deploy_out
    url = _url(context)
    assert url and url["AuthType"] == "NONE", url


@then("the group {group} has a one-time schedule at {at} that deletes that URL")
def step_schedule(context, group, at):
    s = _moto(context)["sess"].client("scheduler").get_schedule(Name="recipe-url-off", GroupName=group)
    assert s["ScheduleExpression"] == f"at({at})", s
    assert s["Target"]["Arn"] == "arn:aws:scheduler:::aws-sdk:lambda:deleteFunctionUrlConfig", s["Target"]
    assert json.loads(s["Target"]["Input"]) == {"FunctionName": "recipe-page"}
    assert s.get("ActionAfterCompletion") == "DELETE", s


@then("run.sh url-on fails")
def step_url_on_fails(context):
    assert context.deploy_code == 1, context.deploy_out


@then("the page function does not have a function URL")
def step_no_url(context):
    assert _url(context) is None


@then("the group {group} does not hold a one-time schedule")
def step_no_schedule(context, group):
    names = [s["Name"] for s in _moto(context)["sess"].client("scheduler").list_schedules(GroupName=group)["Schedules"]]
    assert "recipe-url-off" not in names, names


# ---- the slot list

@then("the JSON has 96 slots in slot order")
def step_96(context):
    starts = [s["slot_start"] for s in context.json["slots"]]
    assert len(starts) == 96 and starts == sorted(starts) and starts[0] == "2026-11-22T00:00Z", starts[:3]


def _slot(context, hhmm):
    return next(s for s in context.json["slots"] if s["slot_start"][11:16] == hhmm)["slot_state"]


@then("slots 08:00, 08:15, 08:30 and 08:45 are loaded, late, loading and failed")
def step_slot_states(context):
    assert [_slot(context, t) for t in ("08:00", "08:15", "08:30", "08:45")] == ["loaded", "late", "loading", "failed"]


@then("slot 09:00 is missing and slots 09:15 and 09:30 are not due")
def step_slot_edges(context):
    assert [_slot(context, t) for t in ("09:00", "09:15", "09:30")] == ["missing", "not due", "not due"]


@then("{m:d} slots are missing and {n:d} are not due")
def step_slot_counts(context, m, n):
    states = [s["slot_state"] for s in context.json["slots"]]
    assert (states.count("missing"), states.count("not due")) == (m, n), (states.count("missing"), states.count("not due"))


# ---- bench.py's cold read (R111): a stand-in for the Lambda API and for the reads

class _FakeLambda:
    def __init__(self, env):
        self.env, self.polls = dict(env), 0

    def get_function_configuration(self, FunctionName):
        self.polls += 1
        return {"Environment": {"Variables": dict(self.env)},
                "LastUpdateStatus": "InProgress" if self.polls == 2 else "Successful"}

    def update_function_configuration(self, FunctionName, Environment):
        self.env = Environment["Variables"]


@given('the page\'s function "{fn}" has the variables PREFIX and COUNTS_TABLE')
def step_bench_fn(context, fn):
    context.bench_fn = fn
    context.bench_lam = _FakeLambda({"PREFIX": "recipe", "COUNTS_TABLE": "recipe-zone-counts"})


@when("bench.py runs and the first read takes {cold:d} ms and the next 100 take {warm:d} ms each")
@when("bench.py runs and the first read takes {cold:d},{rest:d} ms and the next 100 take {warm:d} ms each")
def step_bench_run(context, cold, warm, rest=None):
    import bench
    cold_ms = float(f"{cold}{rest:03d}") if rest is not None else float(cold)
    answers = iter([(cold_ms, 200)] + [(float(warm), 200)] * 100)
    context.bench_out = bench.bench("http://x/", reads=100, every_s=0, read=lambda url: next(answers),
                                    sleep=lambda s: None,
                                    cold=lambda: bench.force_cold(context.bench_lam, context.bench_fn,
                                                                  sleep=lambda s: None))


@then("the function's variables are PREFIX, COUNTS_TABLE and a new BENCH_COLD_START")
def step_bench_env(context):
    env = context.bench_lam.env
    assert sorted(env) == ["BENCH_COLD_START", "COUNTS_TABLE", "PREFIX"], env
    assert env["COUNTS_TABLE"] == "recipe-zone-counts"


@then("bench.py reports the cold read {cold:g} ms and the p95 {p95:g} ms, and {verdict}")
def step_bench_verdict(context, cold, p95, verdict):
    out = context.bench_out
    assert (out["cold_ms"], out["p95_ms"]) == (cold, p95), out
    assert out["passed"] is (verdict == "passes"), out


# ---- live (excluded by default: behave.ini)

@given('the page is deployed with "{a}" and "{b}" has run')
@when('"{a}" runs and bench.py reads the URL 100 times')
@then("the cold read and the p95 are under 1,000 ms and the URL is gone six minutes later")
def step_live(context, **_):
    raise AssertionError("a @live example: run it in a sandbox by hand, README 'Run it live'")


# ---- the page in headless Chromium (playwright); environment.py skips these without Chromium

CHART_STUB = """
window.__charts = [];
window.Chart = class { constructor(el, cfg) { this.config = cfg; this.data = cfg.data; window.__charts.push(this); }
                       update() {} destroy() {} };
"""


def _browser(context, at):
    """serve_local.py on a free port at a fixed read time, and a headless page with a fake clock at `at`."""
    import threading
    import serve_local
    from playwright.sync_api import sync_playwright
    server = serve_local.make_server(port=0, now=_t(at))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    page = browser.new_page()
    context.add_cleanup(lambda: (browser.close(), pw.stop(), server.shutdown()))
    reads = []
    page.on("request", lambda r: reads.append(r.url) if "/api/zone-counts" in r.url else None)
    # the stand-in for Chart.js records what the page draws; the page's integrity check is for the real file only
    page.route("https://cdnjs.cloudflare.com/**", lambda route: route.fulfill(
        body=CHART_STUB, content_type="application/javascript"))

    def without_integrity(route):
        r = route.fetch()
        route.fulfill(response=r, body=re.sub(r'\s+integrity="[^"]*"', "", r.text()))
    page.route(f"{base}/", without_integrity)
    page.clock.install(time=_t(at))
    page.goto(f"{base}/")
    page.wait_for_function("document.getElementById('freshness').textContent !== 'loading…'")
    context.browser_page, context.reads = page, reads


@when("the page is opened in a headless browser at {at} and its first read of /api/zone-counts has answered")
def step_open(context, at):
    _browser(context, at)
    context.drawn = context.browser_page.evaluate(
        "window.__charts.map(c => ({labels: c.data.labels, sets: c.data.datasets.map(d => "
        "({label: d.label, n: d.data.length, dash: d.borderDash}))}))")


@then("the page has 1 chart with 3 series, for zones 48, 161 and 230, of 12 periods each")
def step_chart(context):
    assert len(context.drawn) == 1, context.drawn
    sets = context.drawn[0]["sets"]
    assert [s["label"].split(" (")[0] for s in sets] == ["zone 48", "zone 161", "zone 230"], sets
    assert all(s["n"] == 12 for s in sets) and len(context.drawn[0]["labels"]) == 12, sets


@then('the freshness line reads "{text}"')
def step_freshness(context, text):
    got = context.browser_page.text_content("#freshness")
    assert got == text, got


@then('the series of zone 48 is drawn dashed with "{text}"')
def step_dashed(context, text):
    s = context.drawn[0]["sets"][0]
    assert s["dash"] and text in s["label"], s


@when("the page is open in a headless browser for {seconds:d} seconds on a fake clock")
def step_open_for(context, seconds):
    import time
    _browser(context, "2026-11-23T10:00:30Z")
    context.browser_page.clock.run_for(seconds * 1000)
    want = 1 + seconds // 60
    for _ in range(50):                    # the fetches the timers started answer in real time
        context.browser_page.evaluate("1")      # lets playwright hand over the request events it has
        if len(context.reads) >= want:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    context.browser_page.evaluate("1")


@then("the page has read /api/zone-counts {n:d} times: on load and at 60, 120 and 180 seconds")
def step_reads(context, n):
    assert len(context.reads) == n, context.reads


@then("zone 48 has 4710 for 2026-11-21, 4688 for 2026-11-22 and null for the other 10 days")
def step_lake48(context):
    values = _zone(context, 48)["values"]
    assert (_value(context, 48, "2026-11-21"), _value(context, 48, "2026-11-22")) == (4710, 4688), values
    assert sum(v is None for v in values) == 10, values


@then("zone 161 has 6301 for 2026-11-22, as_of {as_of}, age {age:d} minutes, not stale")
def step_lake161(context, as_of, age):
    assert _value(context, 161, "2026-11-22") == 6301
    _check_zone(context, 161, as_of, age, False)


@then("zone 230 has null for all 12 periods, as_of null, and is stale")
def step_lake230(context):
    z = _zone(context, 230)
    assert z["values"] == [None] * 12 and z["as_of"] is None and z["stale"] is True, z
