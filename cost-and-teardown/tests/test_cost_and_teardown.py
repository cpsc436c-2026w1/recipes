"""cost-and-teardown offline: the cost arithmetic, the unit readers, the inventory's routes, the counting function,
the clean-up and the teardown's order, in moto."""
import base64
import importlib.util
import json
from pathlib import Path

import pytest

import cost
import inventory
import kinds
import units

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def team(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFIX", "t00")
    monkeypatch.setenv("TEAM", "t00")
    monkeypatch.setenv("RESOURCES_DOC", str(tmp_path / "resources.md"))
    monkeypatch.setenv("EVIDENCE", str(tmp_path / "evidence"))
    monkeypatch.setattr("time.sleep", lambda s: None)


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_athena_bills_at_least_10_mb_a_query():
    p = cost.prices()
    rows = cost.run_rows([{"part": "Athena Data scanned, MB", "units": 25, "queries": 3}], p)
    assert rows[0]["part"] == "Athena, billed 30 MB" and abs(rows[0]["usd"] - 30 * 5.5 / 2 ** 20) < 1e-12


def test_not_measured_and_unpriced_rows_make_a_subtotal():
    rows = cost.run_rows([{"not measured": "Application Load Balancer LCU-hours"},
                          {"part": "Lambda requests", "units": 10}], cost.prices())
    assert rows[0]["note"] == "not measured" and cost.total(rows) == (pytest.approx(0.000002), False)


def test_the_horizon_passes_the_limit_in_the_right_month():
    h = cost.horizon([("shard", 1, 0.0165)], 0.003118, busy_months=4, days=365, limit=30)
    assert h["always_on"]["passes_in"] == 3 and round(h["always_on"]["total"], 2) == 144.91
    assert h["demo"]["passes_in"] is None and round(h["demo"]["total"], 2) == 0.78
    assert cost.ordinal(3) == "3rd" and cost.ordinal(11) == "11th" and cost.ordinal(22) == "22nd"


def test_report_lines_and_glue_runs():
    lines = ["REPORT RequestId: a\tDuration: 1.0 ms\tBilled Duration: 1000 ms\tMemory Size: 1024 MB\t",
             "START RequestId: b", "REPORT RequestId: c\tDuration: 1.0 ms\tBilled Duration: 500 ms\tMemory Size: 512 MB"]
    req, gb_s = units.lambda_units(lines)
    assert req["units"] == 2 and gb_s["units"] == 1.25
    assert units.glue_dpu_hours({"DPUSeconds": 7200}) == 2
    assert units.glue_dpu_hours({"ExecutionTime": 600, "NumberOfWorkers": 10, "WorkerType": "G.2X"}) == pytest.approx(10 / 3)


def test_shard_hours_count_a_part_of_an_hour_as_an_hour():
    assert units.shard_hours("2026-11-23T09:00:00Z", "2026-11-23T09:06:00Z") == 1
    assert units.shard_hours("2026-11-23T09:00:00Z", "2026-11-23T11:00:01Z") == 3


def test_arns_and_kinds():
    assert kinds.from_arn("arn:aws:glue:ca-central-1:111122223333:table/t00_cost/trips") == ("glue-table", "t00_cost.trips")
    assert kinds.from_arn("arn:aws:lambda:ca-central-1:111122223333:event-source-mapping:ab-12") == ("lambda-esm", "ab-12")
    assert kinds.from_arn("arn:aws:appsync:ca-central-1:111122223333:apis/xyz")[0] == "appsync:apis"
    assert kinds.canonical("arn:aws:logs:ca-central-1:111122223333:log-group:/aws/lambda/f:*").endswith("group:/aws/lambda/f")
    assert kinds.bills("kinesis-stream") == "hourly" and kinds.bills("s3-bucket") == "monthly"
    assert kinds.bills("lambda-function") == "per use"


def test_demo_up_then_every_route_runs_without_refusal(aws, team):
    demo_up = load("scripts/demo_up.py", "demo_up_t")
    demo_up.up_all(aws, log=lambda *a: None)
    lines = []
    rows = inventory.take(aws, log=lines.append)
    assert not lines, lines
    assert len([r for r in rows if r.in_ledger]) == 10
    role = next(r for r in rows if r.kind == "iam-role")
    assert set(role.found_by) == {"ledger", "IAM"}


def test_a_second_demo_up_borrows_what_is_there(aws, team):
    demo_up = load("scripts/demo_up.py", "demo_up_t2")
    demo_up.up_all(aws, log=lambda *a: None)
    demo_up.up_all(aws, log=lambda *a: None)
    import tags
    assert tags.recorded("kinesis-stream", "t00-cost-events") == "borrowed"


def test_the_counting_function_counts_a_repeated_request_once(aws, team, monkeypatch):
    ddb = aws.client("dynamodb")
    ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    monkeypatch.setenv("TABLE", "t")
    handler = load("function/handler.py", "handler_t")
    handler.TABLE = "t"

    def rec(d):
        return {"kinesis": {"data": base64.b64encode(json.dumps(d).encode()).decode()}}
    req = {"event_type": "trip_requested", "event_id": "e1", "trip_id": "t1", "pickup_zone_id": 48}
    out = handler.handler({"Records": [rec(req), rec(req), rec({"event_type": "trip_accepted", "event_id": "e2"})]})
    assert out == {"transactions": 2, "cancelled": 1}
    assert ddb.get_item(TableName="t", Key={"pk": {"S": "zone#48"}})["Item"]["requested"] == {"N": "1"}


def test_clean_up_dry_run_deletes_nothing_and_keeps_borrowed(aws, team):
    demo_up = load("scripts/demo_up.py", "demo_up_t3")
    aws.client("dynamodb").create_table(TableName="t00-cost-counts", BillingMode="PAY_PER_REQUEST",
                                        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                                        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    demo_up.up_all(aws, log=lambda *a: None)
    clean = load("clean_up.py", "clean_t")
    lines = []
    assert clean.clean(aws, yes=False, log=lines.append) == 0
    assert "t00-cost-events" in aws.client("kinesis").list_streams()["StreamNames"]
    assert clean.clean(aws, yes=True, log=lines.append) == 0
    assert aws.client("kinesis").list_streams()["StreamNames"] == []
    assert "t00-cost-counts" in aws.client("dynamodb").list_tables()["TableNames"]          # borrowed: kept
    assert any(ln.startswith("kept     DynamoDB table t00-cost-counts") for ln in lines)


def test_teardown_steps_run_hourly_first_and_the_bucket_last(tmp_path):
    teardown = load("scripts/teardown.py", "teardown_t")
    path = tmp_path / "t.toml"
    path.write_text('[[step]]\nname = "bucket"\ncommand = "x"\nlast = true\n[[step]]\nname = "per use"\n'
                    'command = "y"\n[[step]]\nname = "stream"\ncommand = "z"\nhourly = true\n')
    assert [s["name"] for s in teardown.steps(path)] == ["stream", "per use", "bucket"]


def test_ledger_rows_of_another_account_or_prefix_are_not_taken(monkeypatch, tmp_path):
    """A copied or old ledger: its rows name resources of another account or PREFIX, which the teardown must not
    delete by name in this account."""
    import inventory
    import tags
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    here, old = "111122223333", "4444" + "55556666"          # two accounts: this one and an earlier lease's
    tags.record("kinesis-stream", "recipe-test-mine", "created",
                arn=f"arn:aws:kinesis:ca-central-1:{here}:stream/recipe-test-mine")
    tags.record("kinesis-stream", "recipe-test-old", "created",
                arn=f"arn:aws:kinesis:ca-central-1:{old}:stream/recipe-test-old")
    monkeypatch.setenv("PREFIX", "someone-else")
    tags.record("dynamodb-table", "someone-else-t", "created")
    monkeypatch.setenv("PREFIX", "recipe-test")
    out = []
    rows = inventory._ledger(here, out.append)
    assert [r.name for r in rows.values()] == ["recipe-test-mine"]
    assert len(out) == 2 and all(line.startswith("skipped  ledger row") for line in out)
