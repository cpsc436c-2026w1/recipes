"""ext-dashboard-page offline: the page's routes, the lake job's conditional writes, the deploy in a dry run and
against moto, and clean_up.py. The build tests the extension's README names are test_slots_stream_variant and
test_late_refresh_counts_unchanged."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from conftest import RECIPE

EXT = RECIPE / "ext-dashboard-page"
sys.path[:0] = [str(EXT), str(RECIPE / "scripts"), str(RECIPE / "sim")]
AT = datetime(2026, 11, 23, 10, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """moto with the counts table; page.py with a client made inside it; a ledger of the test's own."""
    import boto3
    from moto import mock_aws
    for k in ("SLOTS_TABLE", "PERIOD", "STALE_MINUTES", "ZONES", "COUNTS_TABLE", "PURPOSE", "DRY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PREFIX", "recipe")
    monkeypatch.setenv("OWNER", "tester")
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    with mock_aws():
        import load_counts
        import page
        page._ddb = None
        sess = boto3.session.Session(region_name="ca-central-1")
        load_counts.ensure_table(sess, log=lambda *_: None)
        yield {"sess": sess, "ddb": sess.client("dynamodb"), "page": page, "load": load_counts}
        page._ddb = None


def get(page, path, method="GET", qs=None, now=AT):
    out = page.handle({"rawPath": path, "queryStringParameters": qs, "requestContext": {"http": {"method": method}}},
                      now=now)
    return out["statusCode"], (json.loads(out["body"]) if out["headers"]["content-type"].startswith("application/json")
                               else out["body"])


def test_rush_hour_counts_and_freshness(dash):
    out = dash["load"].load("rush-hour", None, dash["sess"], log=lambda *_: None, run_id="t1")
    assert (out["requests"], out["count_items"], out["latest_items"]) == (837, 36, 3)
    status, body = get(dash["page"], "/api/zone-counts")
    assert status == 200 and [sum(z["values"]) for z in body["zones"]] == [304, 191, 342]
    assert body["data_as_of"] == "2026-11-23T09:59:58Z" and body["stale"] is False
    assert [op for op, _ in dash["page"].CALLS] == ["Query"] * 3


def test_a_repeated_delivery_is_counted_once(dash):
    """SIM01 has 4 requests delivered twice: counting by event_id gives 837, not 841."""
    dash["load"].load("rush-hour", None, dash["sess"], log=lambda *_: None, run_id="t2")
    files = sorted((RECIPE / "sim" / "data" / "SIM01").glob("trip-events/dt=*/hour=*/deliveries.jsonl"))
    requested = sum(1 for f in files for l in f.read_text().splitlines() if '"trip_requested"' in l)
    assert requested > 837
    _, body = get(dash["page"], "/api/zone-counts")
    assert sum(sum(z["values"]) for z in body["zones"]) == 837


def test_end_at_moves_the_hour_to_end_at_the_current_period(dash):
    end = datetime(2026, 12, 1, 15, 7, 12, tzinfo=timezone.utc)
    dash["load"].load("rush-hour", end, dash["sess"], log=lambda *_: None, run_id="t3")
    _, body = get(dash["page"], "/api/zone-counts", now=end)
    assert body["periods"][0] == "2026-12-01T14:05:00Z" and body["periods"][-1] == "2026-12-01T15:00:00Z"
    assert [sum(z["values"]) for z in body["zones"]] == [304, 191, 342] and body["stale"] is False


def test_slots_stream_variant(dash, monkeypatch):
    """Build test: with SLOTS_TABLE set the stream variant answers the day's 96 slots from one Query; without it the
    path is 404 and DynamoDB is not called."""
    status, _ = get(dash["page"], "/api/slots", qs={"day": "2026-11-22"})
    assert status == 404 and dash["page"].CALLS == []
    monkeypatch.setenv("SLOTS_TABLE", "recipe-feed-slots")
    dash["ddb"].create_table(TableName="recipe-feed-slots", BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "slot_day", "KeyType": "HASH"},
                                        {"AttributeName": "slot_start", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "slot_day", "AttributeType": "S"},
                                                   {"AttributeName": "slot_start", "AttributeType": "S"}])
    dash["ddb"].put_item(TableName="recipe-feed-slots", Item={
        "slot_day": {"S": "2026-11-22"}, "slot_start": {"S": "2026-11-22T00:00Z"}, "slot_state": {"S": "loaded"},
        "record_total": {"N": "201"}})
    status, body = get(dash["page"], "/api/slots", qs={"day": "2026-11-22"},
                       now=datetime(2026, 11, 23, 0, 46, tzinfo=timezone.utc))
    assert status == 200 and len(body["slots"]) == 96
    assert body["slots"][0] == {"slot_start": "2026-11-22T00:00Z", "slot_state": "loaded", "record_total": 201}
    assert {s["slot_state"] for s in body["slots"][1:]} == {"missing"}
    assert [(op, p["ConsistentRead"]) for op, p in dash["page"].CALLS] == [("Query", True)]
    _, counts = get(dash["page"], "/api/zone-counts")
    assert counts["slots"] is True


def test_late_refresh_counts_unchanged(dash):
    """Build test: a job that started earlier and writes later changes neither the newer count nor the newer
    latest item (real UpdateItem conditions in moto)."""
    import refresh
    ddb = dash["ddb"]
    ddb.put_item(TableName="recipe-zone-counts", Item={"zone_id": {"N": "48"}, "period_start": {"S": "latest"},
                                                       "newest_event_time": {"S": "2026-11-22T23:59:02Z"}})
    ddb.put_item(TableName="recipe-zone-counts", Item={"zone_id": {"N": "48"}, "period_start": {"S": "2026-11-22"},
                                                       "requested": {"N": "6410"},
                                                       "generation": {"S": "2026-11-23T00:00:05Z"}})
    out = refresh.write([{"pickup_zone_id": "48", "period_start": "2026-11-22", "trips_requested": "6120",
                          "newest_requested_time": "2026-11-22 22:10:00.000"}],
                        "2026-11-22T23:00:04Z", ddb, refresh.settings())
    assert out["count_kept"] == 1 and out["latest_kept"] == 1 and out["count_written"] == 0
    item = ddb.get_item(TableName="recipe-zone-counts", Key={"zone_id": {"N": "48"}, "period_start": {"S": "2026-11-22"}})
    assert item["Item"]["requested"]["N"] == "6410"
    latest = ddb.get_item(TableName="recipe-zone-counts", Key={"zone_id": {"N": "48"}, "period_start": {"S": "latest"}})
    assert latest["Item"]["newest_event_time"]["S"] == "2026-11-22T23:59:02Z"
    # a newer run does write
    out = refresh.write([{"pickup_zone_id": "48", "period_start": "2026-11-22", "trips_requested": "6500",
                          "newest_requested_time": "2026-11-23 00:30:00.000"}], "2026-11-23T01:00:00Z", ddb,
                        refresh.settings())
    assert out["count_written"] == 1 and out["latest_written"] == 1


def test_summary_sql_reads_twelve_days_and_prunes_trips_by_day():
    import refresh
    sql = refresh.summary_sql(datetime(2026, 11, 23, 1, tzinfo=timezone.utc), refresh.settings())
    assert "DATE '2026-11-11'" in sql and "DATE '2026-11-23'" in sql
    assert "requested_time >= TIMESTAMP '2026-11-11 00:00:00'" in sql
    with pytest.raises(ValueError):
        refresh.summary_sql(AT, refresh.settings() | {"period": refresh.parse_period("5m")})


def test_unknown_routes_call_no_dynamodb(dash):
    for method, path, want in (("POST", "/api/zone-counts", 405), ("GET", "/api/trips", 404), ("DELETE", "/", 405),
                               ("GET", "/api/slots", 404)):
        assert get(dash["page"], path, method)[0] == want
        assert dash["page"].CALLS == []


def test_in_lambda_the_clients_are_made_at_import(dash, monkeypatch):
    """The Init phase makes the clients (README, Decisions: cold read); outside Lambda nothing is made at import."""
    import importlib
    import refresh
    page = dash["page"]
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    try:
        importlib.reload(page)
        importlib.reload(refresh)
        assert page._ddb is None and refresh._clients == {}
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "recipe-page")
        importlib.reload(page)
        importlib.reload(refresh)
        assert page._ddb is not None and page.CALLS == []
        assert sorted(refresh._clients) == ["athena", "dynamodb"]
        status, body = get(page, "/api/zone-counts")
        assert status == 200 and [op for op, _ in page.CALLS] == ["Query"] * 3
    finally:
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        importlib.reload(page)
        importlib.reload(refresh)


def test_deploy_twice_against_moto_keeps_what_is_there(dash):
    import deploy
    lines = []
    assert deploy.main(["up", "--lake"], sess=dash["sess"], log=lines.append, sleep=lambda s: None) == 0
    assert deploy.main(["up", "--lake"], sess=dash["sess"], log=lines.append, sleep=lambda s: None) == 0
    lam = dash["sess"].client("lambda")
    f = lam.get_function_configuration(FunctionName="recipe-page")
    assert (f["Runtime"], f["Architectures"], f["MemorySize"], f["Timeout"]) == ("python3.12", ["arm64"], 512, 10)
    assert lam.get_function_configuration(FunctionName="recipe-dash-refresh")["MemorySize"] == 128
    job = lam.get_function_configuration(FunctionName="recipe-dash-refresh")
    assert job["Timeout"] == 120 and job["Environment"]["Variables"]["PERIOD"] == "1d"
    assert lam.get_function_event_invoke_config(FunctionName="recipe-dash-refresh")["MaximumRetryAttempts"] == 0
    tags = lam.list_tags(Resource=f["FunctionArn"])["Tags"]
    assert {"Project": "436c", "Team": "recipe", "Owner": "tester"}.items() <= tags.items()
    policy = json.loads(json.dumps(dash["sess"].client("iam").get_role_policy(
        RoleName="recipe-page-role", PolicyName="dashboard-page")["PolicyDocument"]))
    actions = {a for s in policy["Statement"] for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert actions == {"dynamodb:Query", "logs:CreateLogStream", "logs:PutLogEvents"}
    assert "(there already: kept)" in "\n".join(lines)


def test_url_on_makes_the_schedule_first(dash):
    import deploy
    order = []
    dash["sess"].events.register("before-call.*.*", lambda model, **_: order.append(model.name))
    assert deploy.main(["up"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0
    order.clear()
    assert deploy.main(["url-on", "0.1"], sess=dash["sess"], now=AT, log=lambda *_: None) == 0
    assert order.index("CreateSchedule") < order.index("CreateFunctionUrlConfig")
    s = dash["sess"].client("scheduler").get_schedule(Name="recipe-url-off", GroupName="recipe-dashboard-page")
    assert s["ScheduleExpression"] == "at(2026-11-23T10:06:30)" and s["ScheduleExpressionTimezone"] == "UTC"


def test_url_on_deletes_its_schedule_when_the_url_is_refused(dash):
    import deploy
    from botocore.exceptions import ClientError
    assert deploy.main(["up"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0

    def refuse(**_):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "test"}}, "CreateFunctionUrlConfig")
    dash["sess"].events.register("before-call.lambda.CreateFunctionUrlConfig", refuse)
    assert deploy.main(["url-on", "3"], sess=dash["sess"], now=AT, log=lambda *_: None) == 1
    names = [x["Name"] for x in dash["sess"].client("scheduler").list_schedules(GroupName="recipe-dashboard-page")["Schedules"]]
    assert names == []


def test_clean_up_removes_what_the_ledger_created_and_keeps_borrowed(dash):
    import clean_up
    import deploy
    assert deploy.main(["up", "--lake"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0
    assert deploy.main(["url-on", "3"], sess=dash["sess"], now=AT, log=lambda *_: None) == 0
    dry = clean_up.clean(dash["sess"], yes=False, log=lambda *_: None, main_api=False)
    assert not dry.failed and any("function URL" in r for r in dry.removed)
    steps = clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, main_api=False)
    assert not steps.failed, steps.failed
    assert steps.removed[0] == "function URL of recipe-page"
    assert clean_up.check(dash["sess"], log=lambda *_: None) == 0
    again = clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, main_api=False)
    assert not again.failed and not again.removed


def test_clean_up_keeps_a_borrowed_counts_table(dash, tmp_path, monkeypatch):
    import clean_up
    import tags
    monkeypatch.setenv("LEDGER", str(tmp_path / "other-ledger.jsonl"))
    tags.record("dynamodb-table", "recipe-zone-counts", "borrowed")
    steps = clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, main_api=False)
    assert "DynamoDB table recipe-zone-counts (kept: borrowed, not created by this ledger)" in steps.absent
    assert dash["ddb"].describe_table(TableName="recipe-zone-counts")


@pytest.mark.parametrize("args", [["--lake"], ["url-on", "3"], ["url-off"], ["load", "rush-hour"]])
def test_run_sh_dry_calls_nothing(args, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "aws").write_text("#!/bin/sh\necho aws was called >&2\nexit 99\n")
    (fake / "aws").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")} | {
        "DRY": "1", "PATH": f"{fake}:{os.environ['PATH']}", "PY": sys.executable, "LEDGER": str(tmp_path / "l.jsonl"),
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_ENDPOINT_URL": "http://127.0.0.1:9"}
    p = subprocess.run(["bash", str(EXT / "run.sh"), *args], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "aws was called" not in p.stderr and not (tmp_path / "l.jsonl").exists()
    if args[0] != "load":
        assert "$ aws " in p.stdout


def test_bench_counts_a_failure_as_over_the_limit():
    import bench
    answers = iter([(120.0, 200)] * 94 + [(5.0, 503)] * 6)
    out = bench.bench("http://x/", reads=100, every_s=0, read=lambda url: next(answers), sleep=lambda s: None)
    assert out["failed"] == 6 and out["passed"] is False
    answers = iter([(120.0, 200)] * 99 + [(900.0, 200)])
    out = bench.bench("http://x/", reads=100, every_s=0, read=lambda url: next(answers), sleep=lambda s: None)
    assert out["p95_ms"] == 120.0 and out["passed"] is True


class FakeLambda:
    """get_function_configuration / update_function_configuration of one function; the update takes two polls."""

    def __init__(self, env):
        self.env, self.updates, self.polls = dict(env), [], 0

    def get_function_configuration(self, FunctionName):
        self.polls += 1
        return {"Environment": {"Variables": dict(self.env)},
                "LastUpdateStatus": "InProgress" if self.polls == 2 else "Successful"}

    def update_function_configuration(self, FunctionName, Environment):
        self.updates.append(FunctionName)
        self.env = Environment["Variables"]


def test_bench_forces_a_cold_start_and_keeps_the_other_variables():
    import bench
    lam = FakeLambda({"PREFIX": "recipe", "COUNTS_TABLE": "recipe-zone-counts"})
    waits = []
    bench.force_cold(lam, "recipe-page", sleep=waits.append, now=lambda: AT)
    assert lam.updates == ["recipe-page"] and waits == [1]
    assert lam.env == {"PREFIX": "recipe", "COUNTS_TABLE": "recipe-zone-counts",
                       "BENCH_COLD_START": "2026-11-23T10:00:30.000000Z"}


def test_bench_judges_the_cold_read_on_its_own():
    import bench
    for cold_ms, passed in ((1200.0, False), (640.0, True)):
        forced = []
        answers = iter([(cold_ms, 200)] + [(100.0, 200)] * 100)
        out = bench.bench("http://x/", reads=100, every_s=0, read=lambda url: next(answers), sleep=lambda s: None,
                          cold=lambda: forced.append(1))
        assert forced == [1] and out["reads"] == 100 and out["p95_ms"] == 100.0
        assert (out["cold_ms"], out["cold_passed"], out["passed"]) == (cold_ms, passed, passed)
    assert bench.is_local("http://localhost:8080/") and not bench.is_local("https://x.lambda-url.ca-central-1.on.aws/")


def test_memory_mb_sets_the_page_memory_on_create_and_on_update(dash, monkeypatch):
    import deploy
    monkeypatch.setenv("MEMORY_MB", "512")
    assert deploy.main(["up"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0
    lam = dash["sess"].client("lambda")
    assert lam.get_function_configuration(FunctionName="recipe-page")["MemorySize"] == 512
    monkeypatch.setenv("MEMORY_MB", "1024")
    assert deploy.main(["up"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0
    assert lam.get_function_configuration(FunctionName="recipe-page")["MemorySize"] == 1024
    monkeypatch.setenv("MEMORY_MB", "64")
    with pytest.raises(SystemExit):
        deploy.page_memory()


def test_the_page_pins_the_chart_library_with_its_hash():
    html = (EXT / "page.html").read_text()
    assert "Chart.js/4.4.1/chart.umd.min.js" in html and 'integrity="sha512-' in html


def test_load_counts_handle_is_a_replayer_local_target(dash):
    """`--to local --target load_counts:handle`: the replayer's own sender calls handle with each batch."""
    import load_counts
    import replay
    load_counts._state.clear()
    deliveries = replay.load_deliveries("SIM01", RECIPE / "sim" / "data")
    summary = replay.replay(deliveries, replay.make_sender("local", "load_counts:handle"), "t-local", 0.0)
    assert summary["failed"] == 0 and summary["sent"] == len(deliveries)
    _, body = get(dash["page"], "/api/zone-counts")
    assert [sum(z["values"]) for z in body["zones"]] == [304, 191, 342]
    load_counts._state.clear()


def test_operations_the_extension_calls(dash):
    """Every AWS operation up --lake, load, url-on, url-off and the clean-up make: the README's permission rows."""
    import clean_up
    import deploy
    ops = set()
    dash["sess"].events.register("before-call.*.*", lambda model, **_: ops.add(f"{model.service_model.endpoint_prefix}:{model.name}"))
    deploy.main(["up", "--lake"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None)
    dash["load"].load("rush-hour", None, dash["sess"], log=lambda *_: None, run_id="t-ops")
    deploy.main(["url-on", "3"], sess=dash["sess"], now=AT, log=lambda *_: None)
    deploy.main(["url-off"], sess=dash["sess"], log=lambda *_: None)
    clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, main_api=False)
    clean_up.check(dash["sess"], log=lambda *_: None)
    row = next(l for l in (RECIPE / "README.md").read_text().splitlines() if l.startswith("| `ext-dashboard-page/` adds"))
    missing = [op for op in sorted(ops) if f"`{op.replace('tagging:', 'tag:')}`" not in row]
    assert not missing, f"README, AWS permissions, ext-dashboard-page row lacks {missing}"


def test_clean_up_ext_takes_only_that_extension(dash, monkeypatch, capsys):
    """`clean_up.py --ext dashboard-page --yes` removes the page's resources and leaves main/'s API (clean_up.sh is not
    run) and what an ext-read-path run left; `--ext read-path` touches neither the page nor main/."""
    import subprocess

    import clean_up
    import deploy
    import tags
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert deploy.main(["up", "--lake"], sess=dash["sess"], log=lambda *_: None, sleep=lambda s: None) == 0
    tags.record("dynamodb-items", "recipe-rider-trips:r-busy", "created", script="test")
    page = clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, only="dashboard-page")
    assert not page.failed and any("function" in r for r in page.removed)
    assert ran == [] and not any("r-busy" in r for r in page.removed + page.absent + page.failed)
    read = clean_up.clean(dash["sess"], yes=True, log=lambda *_: None, only="read-path")
    assert ran == [] and any("r-busy" in r for r in read.removed + read.absent + read.failed)
    assert not any("function URL" in r for r in read.removed)
    clean_up.clean(dash["sess"], yes=False, log=lambda *_: None)          # no --ext: main/'s API is on the list
    assert ran == []                                                      # (a bare run never calls clean_up.sh)
    assert clean_up.main(["--ext", "dashboard-page"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("Nothing deleted. Run again with --yes to delete these.")
