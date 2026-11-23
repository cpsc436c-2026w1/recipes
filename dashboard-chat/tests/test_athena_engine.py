"""ext-athena-engine, offline: the parse check in the athena dialect, the query function against the stand-in for
Athena and moto's DynamoDB, the limits (queries, deadline, daily cap), the trace's costs, the eval's scoring, the
reader role's policy, the workgroup from scripts/lake.py, and the clean-up.

Build tests named in the spec: test_chat_workgroup_from_helper, test_late_invocation_deadline_*.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from moto import mock_aws

from athena_engine import AthenaEngine, Deadline, LocalInvoker
from chat_athena import RealClock, ask
from main.sqlcheck import SqlRejected, check_sql
from settings import (DAILY_LIMIT_RESULT, MB, QUERY_LIMIT_RESULT, SCAN_LIMIT_RESULT, Limits, Names, athena_usd,
                      billed_mb)
from standin import SIM01_COUNTS, Offline, ScriptedModel, StandInAthena, VirtualClock, make_sim01

EXT = Path(__file__).resolve().parent.parent / "ext-athena-engine"
ONE_ZONE = "SELECT COUNT(*) AS trips FROM trips WHERE pickup_zone_id = 161 AND requested_day = DATE '2024-03-14'"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, EXT / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    return tmp_path / "ledger.jsonl"


@pytest.fixture
def off(tmp_path, aws):
    clock = VirtualClock()
    return Offline(tmp_path / "data", clock)


# --- R140: the parse check, athena dialect -------------------------------------------------------------------------

REJECTED = [
    "DROP TABLE trips",
    "CREATE TABLE trips_copy AS SELECT * FROM trips",
    "UNLOAD (SELECT * FROM trips) TO 's3://recipe-lake/athena-results/chat-wg/x/'",
    "SELECT COUNT(*) FROM trips; DELETE FROM trips",
    "SELECT table_name FROM information_schema.tables",
    "SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret)",
    "EXPLAIN ANALYZE SELECT * FROM trips",
    "SHOW TABLES",
    "MSCK REPAIR TABLE trips",
    "SELECT * FROM recipe_chat.trips",
    "INSERT INTO trips SELECT * FROM trips",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_athena_dialect_rejects(sql):
    with pytest.raises(SqlRejected):
        check_sql(sql, ["trips", "zones"], dialect="athena")


@pytest.mark.parametrize("sql", [
    "WITH z AS (SELECT * FROM trips WHERE pickup_zone_id = 161) SELECT COUNT(*) AS trips FROM z",
    "WITH a AS (SELECT * FROM trips), b AS (SELECT * FROM a) SELECT COUNT(*) FROM b",
    "WITH z AS (SELECT * FROM trips) SELECT COUNT(*) FROM z AS q",
    "SELECT z.zone_name, COUNT(*) FROM trips t JOIN zones z ON t.pickup_zone_id = z.zone_id GROUP BY 1",
    ONE_ZONE,
])
def test_athena_dialect_lets_selects_through(sql):
    check_sql(sql, ["trips", "zones"], dialect="athena")


def test_cte_named_before_it_is_defined_is_not_a_cte():
    with pytest.raises(SqlRejected, match="unknown table a"):
        check_sql("WITH b AS (SELECT * FROM a), a AS (SELECT * FROM trips) SELECT * FROM b", ["trips"],
                  dialect="athena")


def test_duckdb_is_still_the_default_dialect():
    check_sql("SELECT COUNT(*) FROM orders", ["orders"])
    with pytest.raises(SqlRejected):
        check_sql("SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret)", ["orders"])


# --- the query function (R180, R200) -------------------------------------------------------------------------------

def test_cast_by_column_type():
    from query_fn import cast

    assert cast("7661", "bigint") == 7661 and cast("2.5", "double") == 2.5 and cast("1.25", "decimal(8,2)") == 1.25
    assert cast("true", "boolean") is True and cast(None, "integer") is None and cast("2024-03-14", "date") == "2024-03-14"


def test_query_function_returns_typed_rows_without_the_header(off):
    out = off.invoker({"sql": ONE_ZONE, "deadline": off.clock.now() + 18}, 19)
    assert out["state"] == "SUCCEEDED" and out["columns"] == ["trips"] and out["types"] == ["bigint"]
    assert out["rows"] == [[SIM01_COUNTS[161]]]
    assert out["billed_bytes"] == 10 * MB  # under the minimum: billed 10 MB
    assert off.billed_today_bytes() == 10 * MB  # the 100 MB reserve was settled to what it billed


def test_failed_query_gives_the_reserve_back(off):
    out = off.invoker({"sql": "SELECT no_such_column FROM trips", "deadline": off.clock.now() + 18}, 19)
    assert out["state"] == "FAILED" and out["billed_bytes"] == 0
    assert off.billed_today_bytes() == 0


def test_daily_cap_reserve(off):
    off.set_billed_today_mb(5000)
    assert off.invoker({"sql": ONE_ZONE}, 19)["state"] == "SUCCEEDED"
    off.set_billed_today_mb(5030)
    out = off.invoker({"sql": ONE_ZONE}, 19)
    assert out["state"] == "REFUSED" and out["message"] == DAILY_LIMIT_RESULT
    assert len(off.athena.started) == 1


def test_scan_cutoff_is_reported_and_billed(tmp_path, aws):
    clock = VirtualClock()
    off = Offline(tmp_path, clock, scanned_bytes=300 * MB)
    out = off.invoker({"sql": "SELECT * FROM trips"}, 19)
    assert out["state"] == "CUTOFF" and out["message"] == SCAN_LIMIT_RESULT
    assert out["billed_bytes"] == 100 * MB  # a cancelled query bills what it scanned


def test_role_refusal_is_access_denied_and_bills_nothing(tmp_path, aws):
    off = Offline(tmp_path, VirtualClock(), allowed_workgroups=("test-chat-wg",))
    out = off.invoker({"sql": ONE_ZONE, "workgroup": "primary"}, 19)
    assert out["state"] == "DENIED" and "not authorized" in out["message"]
    assert off.billed_today_bytes() == 0


# The reasons the live R170 run got (output/ext-athena-engine-live.txt): the role's Glue refusals end the query as
# FAILED, after StartQueryExecution went through.
GLUE_REFUSALS = [
    "FAILED: Execution Error, return code 1 from org.apache.hadoop.hive.ql.exec.DDLTask. MetaException(message:User: "
    "arn:aws:sts::111122223333:assumed-role/recipe-chat-chat-reader/recipe-chat-chat-query is not authorized to "
    "perform: glue:DeleteTable on resource: arn:aws:glue:ca-central-1:111122223333:table/recipe_chat_chat/trips)",
    "Insufficient permissions to execute the query. User: arn:aws:sts::111122223333:assumed-role/recipe-chat-chat-"
    "reader/recipe-chat-chat-query is not authorized to perform: glue:CreateTable on resource: arn:aws:glue:...",
]


class _FailingAthena:
    def __init__(self, reason):
        self.reason = reason

    def start_query_execution(self, **_):
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.reason},
                                   "Statistics": {"DataScannedInBytes": 0}, "WorkGroup": "test-chat-wg"}}


class _Usage:
    def __init__(self):
        self.deltas = []

    def update_item(self, **kw):
        if "ConditionExpression" not in kw:
            self.deltas.append(int(kw["ExpressionAttributeValues"][":delta"]["N"]))


@pytest.mark.parametrize("reason", GLUE_REFUSALS)
def test_role_refusal_reported_as_a_failed_query_is_access_denied(reason):
    from query_fn import run_query

    usage = _Usage()
    out = run_query({"sql": "DROP TABLE trips"}, athena=_FailingAthena(reason), usage=usage,
                    env={"WORKGROUP": "test-chat-wg", "USAGE_TABLE": "u", "DATABASE": "d"}, clock=VirtualClock())
    assert out["state"] == "DENIED" and out["message"].startswith("access denied: ")
    assert out["billed_bytes"] == 0 and usage.deltas == [-100 * MB]  # the reserve given back, nothing billed


def test_other_failed_query_stays_failed():
    from query_fn import run_query

    out = run_query({"sql": "SELECT nope FROM trips"}, athena=_FailingAthena("COLUMN_NOT_FOUND: line 1:8: Column "
                                                                             "'nope' cannot be resolved"),
                    usage=_Usage(), env={"WORKGROUP": "w", "USAGE_TABLE": "u", "DATABASE": "d"}, clock=VirtualClock())
    assert out["state"] == "FAILED" and out["message"].startswith("COLUMN_NOT_FOUND")


def test_a_query_waits_at_most_10_s(tmp_path, aws):
    clock = VirtualClock()
    off = Offline(tmp_path, clock, takes_s=None)
    out = off.invoker({"sql": ONE_ZONE, "deadline": clock.now() + 18}, 19)
    assert out["state"] == "STOPPED" and "10 s" in out["message"]
    assert clock.elapsed() == pytest.approx(10, abs=0.01) and off.athena.stops == ["q-0001"]


# --- R180: billed Data scanned and the Athena cost -----------------------------------------------------------------

def test_billed_mb_and_cost():
    billed = [billed_mb("SUCCEEDED", 3 * MB), billed_mb("SUCCEEDED", 40 * MB), billed_mb("FAILED", 0)]
    assert billed == [10, 40, 0]
    assert round(athena_usd(sum(billed) * MB), 6) == 0.000262
    assert billed_mb("SUCCEEDED", 40 * MB + 1) == 41
    assert billed_mb("FAILED", 120 * MB, cancelled_at_cutoff=True) == 120


def test_trace_item_has_scanned_billed_and_both_costs(off, tmp_path):
    from main.traces import LocalTraces

    traces = LocalTraces(tmp_path / "t.jsonl")
    a = ask("q", model=ScriptedModel([ONE_ZONE], off.clock), engine=off.engine, clock=off.clock, traces=traces)
    item = traces.all()[0]
    assert item["status"] == "answered" and item["billed_bytes"] == 10 * MB
    assert item["queries"][0]["data_scanned_bytes"] > 0 and item["athena_usd"] == round(athena_usd(10 * MB), 9)
    assert item["model_cad"] == pytest.approx(a.cost_cad) and "last_rows" not in item


# --- R160: at most 3 queries -----------------------------------------------------------------------------------------

def test_fourth_query_of_one_reply_is_refused(off):
    sqls = [f"SELECT COUNT(*) FROM trips WHERE pickup_zone_id = {z}" for z in (48, 161, 230, 100)]
    a = ask("each zone", model=ScriptedModel(sqls, off.clock), engine=off.engine, clock=off.clock)
    tools = [s for s in a.steps if s.kind == "tool_call"]
    assert len(off.athena.started) == 3
    assert tools[3].status == "rejected" and tools[3].detail.endswith(QUERY_LIMIT_RESULT)


def test_tool_result_is_the_refusal_word_for_word(off):
    from main.chat import ToolRefused, _run_tool
    from main.model import ToolCall

    off.engine.new_question(Deadline(off.clock, 18))
    for _ in range(3):
        off.engine.queries.append({"ran": True})
    from main.chat import Answer

    result = _run_tool(ToolCall("c", "run_sql", {"sql": ONE_ZONE}), off.engine, Answer("r", "q", "u", "x"), 0.0, ())
    assert result == {"error": QUERY_LIMIT_RESULT}
    assert issubclass(ToolRefused, Exception)


# --- R210: the deadline ---------------------------------------------------------------------------------------------

def test_athena_never_finishes(tmp_path, aws):
    clock = VirtualClock()
    off = Offline(tmp_path, clock, takes_s=None)
    sqls = ["SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161", "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 48"]
    a = ask("both", model=ScriptedModel(sqls, clock), engine=off.engine, clock=clock)
    assert (a.status, a.reason) == ("stopped", "deadline")
    assert len(off.athena.started) == 2 and len(off.athena.stops) == 2
    assert clock.elapsed() <= 20


def test_model_call_near_the_deadline(tmp_path, aws):
    clock = VirtualClock()
    off = Offline(tmp_path, clock, takes_s=9)
    model = ScriptedModel(["SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161"], clock, takes_s=5)
    a = ask("zone 161", model=model, engine=off.engine, clock=clock)
    second = model.calls[1]
    assert second["start"] - clock.t0 == pytest.approx(14, abs=0.01)
    assert second["timeout_s"] == pytest.approx(4, abs=0.01)
    assert second["cut_off_at"] - clock.t0 == pytest.approx(18, abs=0.01)
    assert (a.status, a.reason) == ("stopped", "deadline") and clock.elapsed() <= 18.5


def test_late_invocation_deadline_query_at_17_5_s(tmp_path, aws):
    """(a) a query invoked at 17.5 s: the function waits only to the deadline, stops the query, the question stops."""
    clock = VirtualClock()
    off = Offline(tmp_path, clock, takes_s=None)
    model = ScriptedModel(["SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161"], clock, takes_s=17.5)
    model.request_timeout_s = None
    a = ask("late", model=_NoTimeout(model), engine=off.engine, clock=clock)
    assert off.invoker.calls[0]["at"] - clock.t0 == pytest.approx(17.5, abs=0.01)
    assert off.athena.stops == ["q-0001"]
    assert (a.status, a.reason) == ("stopped", "deadline") and clock.elapsed() <= 19


class _NoTimeout:
    """A model whose first reply takes 17.5 s although its timeout is 6 s (a reply already on its way)."""

    def __init__(self, inner):
        self.inner, self.model, self.request_timeout_s = inner, inner.model, None

    def chat(self, messages, tools=None):
        self.inner.request_timeout_s = None
        return self.inner.chat(messages, tools)


SCALE = 0.1  # the wall-clock tests run the same limits shrunk ten times: deadline 1.8 s, model call 0.6 s, query 1 s


def test_late_invocation_deadline_connection_never_answers(tmp_path):
    """(b) the function's invocation never answers: the backstop ends the question at the deadline (wall clock)."""
    limits = Limits().scaled(SCALE)
    release = threading.Event()

    def never(payload, timeout_s):
        release.wait(30)
        return {"state": "SUCCEEDED", "columns": ["x"], "types": ["bigint"], "rows": [[1]], "truncated": None}

    engine = AthenaEngine(never, limits=limits)
    t0 = time.monotonic()
    a = ask("hangs", model=ScriptedModel([ONE_ZONE], RealClock()), engine=engine, limits=limits, clock=RealClock())
    took = time.monotonic() - t0
    release.set()
    assert (a.status, a.reason) == ("stopped", "deadline")
    assert took <= limits.deadline_s * (19 / 18)  # by 1.9 s, the scaled 19 s


class _Trickle(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", "100000")
        self.end_headers()
        try:
            for _ in range(600):  # one byte every 0.1 s: each read is short, so httpx's read timeout never fires
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def trickle_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.mark.parametrize("scale", [SCALE, 1.0], ids=["scaled-1.8s", "full-18s"])
def test_late_invocation_deadline_trickled_model_reply(trickle_url, scale, tmp_path):
    """(c) a model reply trickled one byte at a time: httpx's timeout never fires; the question still ends by the
    deadline, and the worker left behind writes no trace."""
    from main.model import GatewayModel
    from main.traces import LocalTraces

    limits = Limits().scaled(scale)
    model = GatewayModel(trickle_url, "test-token", "qwen3.6-35b-a3b")
    engine = AthenaEngine(lambda p, t: {"state": "FAILED", "message": "unused"}, limits=limits)
    traces = LocalTraces(tmp_path / "t.jsonl")
    t0 = time.monotonic()
    a = ask("trickle", model=model, engine=engine, limits=limits, clock=RealClock(), traces=traces)
    took = time.monotonic() - t0
    assert (a.status, a.reason) == ("stopped", "deadline")
    assert took <= limits.deadline_s * (19 / 18)
    assert len(traces.all()) == 1  # written once, here; the worker that is still reading writes none


# --- R190: the eval's scoring ---------------------------------------------------------------------------------------

def test_rows_compared_typed_and_in_any_order():
    from trip_counts import score

    ref = [[48, 4345], [161, 7661]]
    assert score(ref, "answered", [[161, 7661], [48, 4345]], "48: 4,345; 161: 7,661")[0]
    assert not score(ref, "answered", [[161, 7661]], "")[0]
    assert not score([[7661]], "answered", [[7661]], "The answer is 7660.")[0]
    assert score([[7661]], "answered", [[7661]], "There were 7,661 trips.")[0]
    assert not score([[7661]], "stopped", [[7661]], "7661")[0]


def test_eval_verdict_thresholds():
    from trip_counts import verdict

    results = [{"case": f"c{i}", "correct": not (i < 2 and r == 0) and i != 9} for i in range(10) for r in range(3)]
    v = verdict(results)
    assert v["runs_correct"] == 25 and v["cases_passed"] == 9 and v["passed"]


def test_eval_cases_are_ten_of_the_three_kinds():
    from trip_counts import load_cases

    cases = load_cases()
    kinds = [c["kind"].split(",")[0] for c in cases]
    assert len(cases) == 10 and kinds.count("one zone") == 8 and kinds.count("two zones") == 2
    for c in cases:
        check_sql(c["reference_sql"], ["trips", "zones"], dialect="athena")


# --- R170: the reader role ------------------------------------------------------------------------------------------

def test_reader_policy_is_scoped():
    deploy = _load("athena_deploy", "deploy.py")
    names = Names("test")
    doc = deploy.reader_policy(names, "test-lake-111122223333", "111122223333")
    actions = [a for s in doc["Statement"] for a in s["Action"]]
    assert not any(a.endswith(":*") for a in actions)
    assert not any(a in actions for a in ("glue:CreateTable", "glue:DeleteTable", "s3:DeleteObject"))
    athena = next(s for s in doc["Statement"] if s["Sid"] == "QueriesInTheChatWorkgroupOnly")
    assert athena["Resource"].endswith(":workgroup/test-chat-wg")
    listing = next(s for s in doc["Statement"] if s["Sid"] == "ListOwnPrefixes")
    assert listing["Condition"]["StringLike"]["s3:prefix"] == ["chat/*", "athena-results/chat-wg/*"]
    location = next(s for s in doc["Statement"] if s["Sid"] == "BucketLocation")
    assert "Condition" not in location


def test_package_holds_the_function_and_its_settings():
    import io
    import zipfile

    deploy = _load("athena_deploy", "deploy.py")
    assert sorted(zipfile.ZipFile(io.BytesIO(deploy.package())).namelist()) == ["query_fn.py", "settings.py"]


# --- the workgroup from scripts/lake.py (build test) ----------------------------------------------------------------

def test_chat_workgroup_from_helper(aws, ledger):
    import boto3
    import lake

    sess = boto3.Session(region_name="ca-central-1")
    lake.up(sess, log=lambda *a: None)
    assert lake.ensure_workgroup("test-chat-wg", sess=sess) == "created"
    cfg = sess.client("athena").get_work_group(WorkGroup="test-chat-wg")["WorkGroup"]["Configuration"]
    assert cfg["BytesScannedCutoffPerQuery"] == 104_857_600 and cfg["EnforceWorkGroupConfiguration"] is True
    assert cfg["ResultConfiguration"]["OutputLocation"].endswith("/athena-results/chat-wg/")
    assert cfg["EngineVersion"]["SelectedEngineVersion"] == "Athena engine version 3"
    clean = _load("athena_clean_up", "clean_up.py")
    steps = clean.clean_up(Names("test"), yes=True, sess=sess)
    assert "athena workgroup test-chat-wg" in steps.removed


def test_existing_chat_workgroup_with_1_gb_stops_the_deploy(aws, ledger):
    import boto3
    import lake

    sess = boto3.Session(region_name="ca-central-1")
    lake.up(sess, log=lambda *a: None)
    sess.client("athena").create_work_group(Name="test-chat-wg", Configuration={
        "BytesScannedCutoffPerQuery": 2**30, "EnforceWorkGroupConfiguration": True,
        "ResultConfiguration": {"OutputLocation": f"s3://{lake.bucket_name(sess)}/athena-results/chat-wg/"},
        "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"}})
    with pytest.raises(lake.WorkgroupMismatch, match="(?i)cutoff"):
        lake.ensure_workgroup("test-chat-wg", sess=sess)
    with pytest.raises(ValueError):
        lake.ensure_workgroup("test-chat-wg", cutoff=2**30, sess=sess)


# --- clean-up --------------------------------------------------------------------------------------------------------

def test_clean_up_deletes_what_it_owns_and_keeps_the_bucket(aws, ledger, tmp_path):
    import boto3
    import lake
    from tags import record

    sess = boto3.Session(region_name="ca-central-1")
    names = Names("test")
    lake.up(sess, log=lambda *a: None)
    bucket = lake.bucket_name(sess)
    s3 = sess.client("s3")
    for key in ("chat/trips/requested_day=2024-03-14/part-0.parquet", "athena-results/chat-wg/q.csv",
                "raw/trip-events/dt=2024-03-14/a.jsonl"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    deploy = _load("athena_deploy", "deploy.py")
    iam = sess.client("iam")
    iam.create_role(RoleName=names.role, AssumeRolePolicyDocument=json.dumps(deploy.trust_policy()))
    iam.put_role_policy(RoleName=names.role, PolicyName="reader",
                        PolicyDocument=json.dumps(deploy.reader_policy(names, bucket, "123456789012")))
    from standin import create_usage_table

    create_usage_table(sess.client("dynamodb"), names.usage_table)
    sess.client("logs").create_log_group(logGroupName=names.log_group)
    glue = sess.client("glue")
    glue.create_database(DatabaseInput={"Name": "test_chat"})
    for t in ("trips", "zones"):
        glue.create_table(DatabaseName="test_chat", TableInput={"Name": t})
    sess.client("athena").create_work_group(Name="test-chat-wg", Configuration={})
    record("athena-workgroup", "test-chat-wg", "borrowed")  # made by someone else: kept
    clean = _load("athena_clean_up", "clean_up.py")
    steps = clean.clean_up(names, yes=True, sess=sess)
    assert not steps.failed, steps.failed
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
    assert keys == ["raw/trip-events/dt=2024-03-14/a.jsonl"]
    assert glue.get_databases()["DatabaseList"] == []
    assert iam.list_roles()["Roles"] == []
    assert sess.client("athena").get_work_group(WorkGroup="test-chat-wg")  # borrowed: still there
    s3.head_bucket(Bucket=bucket)  # never the bucket


def test_clean_up_without_yes_contacts_nothing(monkeypatch):
    clean = _load("athena_clean_up", "clean_up.py")
    monkeypatch.setattr(clean.boto3, "Session", lambda **k: (_ for _ in ()).throw(AssertionError("AWS called")))
    steps = clean.clean_up(Names("test"), yes=False)
    assert steps.dry_run and len(steps.removed) == 7


# --- the data load --------------------------------------------------------------------------------------------------

def test_load_keeps_uber_trips_picked_up_in_manhattan_in_march(tmp_path):
    import duckdb

    load = _load("athena_load", "load.py")
    zones = tmp_path / "zones.csv"
    zones.write_text('"LocationID","Borough","Zone","service_zone"\n161,"Manhattan","Midtown Center","Yellow Zone"\n'
                     '132,"Queens","JFK Airport","Airports"\n')
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT * FROM (VALUES
        ('HV0003','B1','B1',TIMESTAMP '2024-03-14 08:00:00',NULL::TIMESTAMP,TIMESTAMP '2024-03-14 08:05:00',
         TIMESTAMP '2024-03-14 08:20:00',161,1,2.0,900,12.5,10.0),
        ('HV0003','B1','B1',TIMESTAMP '2024-03-14 08:00:00',NULL::TIMESTAMP,TIMESTAMP '2024-03-14 08:05:00',
         TIMESTAMP '2024-03-14 08:20:00',161,1,2.0,900,12.5,10.0),
        ('HV0005','B2','B2',TIMESTAMP '2024-03-14 09:00:00',NULL::TIMESTAMP,TIMESTAMP '2024-03-14 09:05:00',
         TIMESTAMP '2024-03-14 09:20:00',161,1,2.0,900,12.5,10.0),
        ('HV0003','B1','B1',TIMESTAMP '2024-03-15 09:00:00',NULL::TIMESTAMP,TIMESTAMP '2024-03-15 09:05:00',
         TIMESTAMP '2024-03-15 09:20:00',132,1,2.0,900,12.5,10.0),
        ('HV0003','B1','B1',TIMESTAMP '2024-02-29 23:59:00',NULL::TIMESTAMP,TIMESTAMP '2024-03-01 00:05:00',
         TIMESTAMP '2024-03-01 00:20:00',161,1,2.0,900,12.5,10.0))
        t(hvfhs_license_num, dispatching_base_num, originating_base_num, request_datetime, on_scene_datetime,
          pickup_datetime, dropoff_datetime, PULocationID, DOLocationID, trip_miles, trip_time,
          base_passenger_fare, driver_pay)) TO '{tmp_path / 'trips.parquet'}' (FORMAT parquet)""")
    counts = load.write_tables(tmp_path / "trips.parquet", zones, tmp_path / "out")
    # DS01 is cut by pickup time: the trip requested on 2024-02-29 and picked up on 2024-03-01 is kept, in its own
    # request day (the live run found 814 such trips; filtering by request time lost them)
    assert counts == {"trips": 3, "days": 2, "zones": 2}
    ids = con.execute(f"SELECT trip_id FROM read_parquet('{tmp_path}/out/trips/**/*.parquet')").fetchall()
    assert len(set(ids)) == 3  # two identical rows, two ids: the row's position is in the hash
    assert (tmp_path / "out" / "trips" / "requested_day=2024-03-14").is_dir()
    assert (tmp_path / "out" / "trips" / "requested_day=2024-02-29").is_dir()


def test_sim01_stand_in_has_the_measured_counts(tmp_path):
    import duckdb

    paths = make_sim01(tmp_path)
    rows = duckdb.sql(f"SELECT pickup_zone_id, COUNT(*) FROM read_parquet('{paths['trips']}/**/*.parquet') "
                      "GROUP BY 1 ORDER BY 1").fetchall()
    assert dict(rows) == SIM01_COUNTS and sum(SIM01_COUNTS.values()) == 19_074


def test_p95_is_the_29th_of_30():
    run = _load("athena_run", "run_athena.py")
    assert run.p95_nearest_rank([float(i) for i in range(1, 31)]) == 29.0


assert socket and StandInAthena and LocalInvoker  # imported for the reader


def test_query_stopped_at_the_deadline_still_reaches_the_trace(tmp_path):
    """The function stops the query at the deadline and returns a moment after: the grace lets its billed bytes
    and the model's cost into the trace (wall clock, scaled)."""
    from main.traces import LocalTraces

    limits = Limits().scaled(SCALE)

    def stops_at_deadline(payload, timeout_s):
        time.sleep(max(0.0, payload["deadline"] - time.time()) + 0.2 * SCALE)
        return {"state": "STOPPED", "message": "deadline", "data_scanned_bytes": 3 * MB, "billed_bytes": 10 * MB,
                "query_execution_id": "q-late"}

    traces = LocalTraces(tmp_path / "t.jsonl")
    a = ask("slow", model=ScriptedModel([ONE_ZONE], RealClock()), engine=AthenaEngine(stops_at_deadline, limits=limits),
            limits=limits, clock=RealClock(), traces=traces)
    item = traces.all()[0]
    assert (a.status, a.reason) == ("stopped", "deadline")
    assert item["billed_bytes"] == 10 * MB and item["queries"][0]["state"] == "STOPPED"
    assert item["model_cad"] > 0 and a.latency_ms / 1000 <= limits.deadline_s * (19 / 18)


def test_a_worker_left_behind_cannot_use_the_next_question(tmp_path):
    limits = Limits().scaled(SCALE)
    gate = threading.Event()
    calls = []

    def invoke(payload, timeout_s):
        calls.append(payload["sql"])
        if len(calls) == 1:
            gate.wait(5)  # the first question's query outlives its question
        return {"state": "SUCCEEDED", "columns": ["n"], "types": ["bigint"], "rows": [[1]], "truncated": None}

    engine = AthenaEngine(invoke, limits=limits)
    sqls = ["SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 1", "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 2"]
    first = ask("first", model=ScriptedModel(sqls, RealClock()), engine=engine, limits=limits, clock=RealClock())
    second_view = engine.new_question(Deadline(RealClock(), 10))
    gate.set()
    time.sleep(0.3)  # the first worker resumes and tries its second tool call
    assert first.status == "stopped" and calls == [sqls[0]] and second_view.queries == []


def test_parquet_types_match_the_ddl(tmp_path):
    """Athena reads a Parquet column by the type the DDL declares; a BIGINT file column under `int` is refused."""
    import duckdb

    load = _load("athena_load", "load.py")
    deploy = _load("athena_deploy", "deploy.py")
    zones = tmp_path / "zones.csv"
    zones.write_text('"LocationID","Borough","Zone","service_zone"\n161,"Manhattan","Midtown Center","Yellow Zone"\n')
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT 'HV0003' AS hvfhs_license_num, 'B1' AS dispatching_base_num, 'B1' AS originating_base_num,
        TIMESTAMP '2024-03-14 08:00:00' AS request_datetime, NULL::TIMESTAMP AS on_scene_datetime,
        TIMESTAMP '2024-03-14 08:05:00' AS pickup_datetime, TIMESTAMP '2024-03-14 08:20:00' AS dropoff_datetime,
        161::BIGINT AS PULocationID, 1::BIGINT AS DOLocationID, 2.0 AS trip_miles, 900::BIGINT AS trip_time,
        12.5 AS base_passenger_fare, 10.0 AS driver_pay) TO '{tmp_path / 't.parquet'}' (FORMAT parquet)""")
    load.write_tables(tmp_path / "t.parquet", zones, tmp_path / "out")
    hive_to_duck = {"string": "VARCHAR", "int": "INTEGER", "double": "DOUBLE", "timestamp": "TIMESTAMP"}
    ddl = deploy.ddl(Names("test"), "b")
    for name, stmt in (("trips", ddl[1]), ("zones", ddl[2])):
        body = stmt.split("(", 1)[1].split(")")[0]
        declared = {c.split()[0]: hive_to_duck[c.split()[1]] for c in body.replace("\n", " ").split(",") if c.strip()}
        files = f"{tmp_path}/out/{name}/**/*.parquet"
        got = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{files}', "
                                               "hive_partitioning = false)").fetchall()}
        assert {c: got[c] for c in declared} == declared, (name, got)


def test_deploy_refuses_a_chat_database_other_than_its_own(monkeypatch):
    """deploy.py builds <prefix>_chat only: with CHAT_DATABASE set to a team's lake it stops before any AWS call."""
    deploy = _load("athena_deploy", "deploy.py")
    monkeypatch.setenv("CHAT_DATABASE", "team_lake")
    names = Names("test")
    assert names.database == "team_lake"

    class NoAws:
        def client(self, *a, **k):
            raise AssertionError("deploy.py called AWS with CHAT_DATABASE set")

    with pytest.raises(SystemExit, match="CHAT_DATABASE"):
        deploy.up("sim01", names, sess=NoAws(), log=lambda *_: None)
