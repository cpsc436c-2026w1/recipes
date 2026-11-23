"""What the live runs of ext-queue-ingest showed was missing or wrong, each fixed and tested here (offline, moto):
the window-0 comparison run Lambda accepts, a second run into one table counted on its own, clean_up.py's dry run,
the SQS requests counted from CloudTrail data events, and the cost computed from measured units only."""
import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import clean_up
import infra

ROOT = Path(__file__).resolve().parent.parent
REGION = "ca-central-1"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load("queue_ingest_run", "ext-queue-ingest/run.py")
latency = _load("queue_ingest_latency", "ext-queue-ingest/latency.py")
request_count = _load("queue_request_count", "ext-queue-ingest/request_count.py")


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))


# ---------- the comparison run without a window

def test_no_window_takes_batches_of_ten_because_lambda_refuses_more_without_a_window():
    assert run.settings("dynamodb", 0) == {"window_s": 0, "batch_size": 10, "visibility_s": 180}
    assert run.settings("dynamodb", 30) == {"window_s": 30, "batch_size": 100, "visibility_s": 210}


def test_a_window_under_30_is_still_refused_as_a_setting():
    with pytest.raises(run.SettingError, match="30 to 60"):
        run.batch_window({"BATCH_WINDOW_S": "0"})


@mock_aws
def test_the_no_window_mapping_has_window_0_and_batch_10():
    s = boto3.Session(region_name=REGION)
    made = run.create(s, "recipe-test", "dynamodb", 0)
    m = s.client("lambda").get_event_source_mapping(UUID=made["mapping"])
    assert (m["MaximumBatchingWindowInSeconds"], m["BatchSize"]) == (0, 10)
    url = s.client("sqs").get_queue_url(QueueName="recipe-test-trip-completed")["QueueUrl"]
    vis = s.client("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["VisibilityTimeout"])["Attributes"]
    assert vis["VisibilityTimeout"] == "180"


def test_no_window_with_the_s3_sink_is_refused_before_anything_is_made(monkeypatch, capsys):
    monkeypatch.setenv("SINK", "s3")
    monkeypatch.setattr(run, "run_aws", lambda *a, **k: pytest.fail("run_aws was called"))
    assert run.main(["--target", "aws", "--no-window"]) == 2
    assert "--no-window is for the DynamoDB sink" in capsys.readouterr().err


# ---------- a second run into the same table

@mock_aws
def test_a_second_run_counts_only_its_own_items():
    ddb = boto3.client("dynamodb", region_name=REGION)
    infra.create_table(ddb, "t", "trip_id", poll_s=0.01)
    for run_id, n in (("run-a", 837), ("run-b", 12)):
        for i in range(n):
            ddb.put_item(TableName="t", Item={"trip_id": {"S": f"{run_id}-t-{i:04d}"}})
    assert run.count_items(ddb, "t") == 849
    assert run.count_items(ddb, "t", "run-b") == 12
    assert run.wait_stored(ddb, "t", 837, wait_s=0, run_id="run-b") == 12      # not "837 of 837" at once


# ---------- clean_up.py --dry-run

@mock_aws
def test_the_dry_run_deletes_nothing_and_lists_what_is_there():
    s = boto3.Session(region_name=REGION)
    run.create(s, "recipe-test", "dynamodb", 30)
    steps = clean_up.clean(s, "recipe-test", dry_run=True)
    assert steps.failed == []
    assert "function recipe-test-trip-completed-consumer" in steps.removed
    assert "role recipe-test-queue-consumer-role" in steps.removed
    assert "queue recipe-test-trip-completed-dlq" in steps.removed
    assert "function recipe-test-consumer" in steps.absent                   # not made: absent, not "would remove"
    assert "role recipe-test-feed-scheduler-role" in steps.absent
    assert [f["FunctionName"] for f in s.client("lambda").list_functions()["Functions"]] == [
        "recipe-test-trip-completed-consumer"]
    assert len(s.client("sqs").list_queues()["QueueUrls"]) == 2
    assert clean_up.clean(s, "recipe-test").failed == []                     # then the real one
    assert s.client("lambda").list_functions()["Functions"] == []


@mock_aws
def test_the_dry_run_does_not_mark_the_run_log_deleted():
    from tags import record, recorded

    s = boto3.Session(region_name=REGION)
    infra.create_table(s.client("dynamodb"), "recipe-test-runs", "job", poll_s=0.01)
    record("dynamodb-table", "recipe-test-runs")
    clean_up.clean(s, "recipe-test", dry_run=True)
    assert recorded("dynamodb-table", "recipe-test-runs") == "created"
    assert "recipe-test-runs" in s.client("dynamodb").list_tables()["TableNames"]


# ---------- SQS requests from CloudTrail data events

def test_requests_are_counted_by_queue_and_action():
    ev = lambda name, q, t="2026-11-23T08:00:00Z": {  # noqa: E731
        "eventTime": t, "eventName": name,
        "resources": [{"type": "AWS::SQS::Queue", "ARN": f"arn:aws:sqs:ca-central-1:111122223333:{q}"}]}
    events = [ev("ReceiveMessage", "p-trip-completed")] * 3 + [ev("DeleteMessageBatch", "p-trip-completed"),
              ev("ReceiveMessage", "p-trip-completed-dlq"), ev("ReceiveMessage", "p-trip-completed",
                                                                "2026-11-23T10:00:00Z")]
    from datetime import datetime, timezone
    got = request_count.count_events(events, end=datetime(2026, 11, 23, 9, tzinfo=timezone.utc))
    assert got[("p-trip-completed", "ReceiveMessage")] == 3
    assert got[("p-trip-completed-dlq", "ReceiveMessage")] == 1
    mine = latency.queue_requests({f"{q} {a}": n for (q, a), n in got.items()}, "p-trip-completed")
    assert mine == {"by_action": {"ReceiveMessage": 3, "DeleteMessageBatch": 1}, "total": 4}


def test_the_trail_logs_only_the_prefix_queues():
    (sel,) = request_count.selectors("ca-central-1", "111122223333", "recipe-queue")
    fields = {f["Field"]: f for f in sel["FieldSelectors"]}
    assert fields["resources.ARN"]["StartsWith"] == ["arn:aws:sqs:ca-central-1:111122223333:recipe-queue"]
    assert fields["eventCategory"]["Equals"] == ["Data"]


@mock_aws
def test_the_trail_and_its_bucket_are_made_and_removed():
    s = boto3.Session(region_name=REGION)
    n = request_count.up(s, "recipe-test")
    assert s.client("cloudtrail").get_trail(Name=n["trail"])["Trail"]["S3BucketName"] == n["bucket"]
    assert request_count.down(s, "recipe-test", dry_run=True)[0] == f"trail {n['trail']}"
    assert clean_up.clean(s, "recipe-test").failed == []
    assert s.client("cloudtrail").describe_trails()["trailList"] == []
    assert n["bucket"] not in [b["Name"] for b in s.client("s3").list_buckets()["Buckets"]]


# ---------- the cost from measured units

def test_the_consumer_and_report_lines_give_calls_sizes_and_billed_time():
    lines = ['{"batch": 3, "stored": 1, "repeat": 1, "other": 1, "failed": 0}', '{"batch": 1, "stored": 1}',
             '{"batch": 8, "other": 8}',
             "REPORT RequestId: a\tDuration: 50.00 ms\tBilled Duration: 51 ms\tMemory Size: 128 MB",
             "REPORT RequestId: b\tDuration: 10.50 ms\tBilled Duration: 11 ms\tMemory Size: 128 MB",
             "REPORT RequestId: c\tDuration: 20.00 ms\tBilled Duration: 20 ms\tMemory Size: 128 MB",
             "START RequestId: a Version: $LATEST"]
    got = latency.batch_lines(lines)
    assert (got["calls"], got["mean_batch"], got["median_batch"], got["billed_ms"], got["writes"]) == (3, 4, 3, 82, 3)


def test_the_month_is_the_run_times_720_over_its_hours():
    units = {"sqs_requests": 1_000_000, "invocations": 0, "billed_ms": 0, "memory_mb": 128, "writes": 0,
             "log_bytes": 0}
    c = latency.costs(units, run_hours=2.0)
    assert c["run_usd"] == pytest.approx(0.40)
    assert c["month_usd"] == pytest.approx(0.40 * 360) and c["year_usd"] == pytest.approx(0.40 * 4380)
    assert c["month_cad"] == pytest.approx(c["month_usd"] * 1.45)
    assert c["lease_pct"] == pytest.approx(100 * 144 / 30)


def test_the_table_puts_runs_side_by_side():
    def measured(window, batch, calls):
        units = {"sqs_requests": 5000, "invocations": calls, "billed_ms": calls * 40, "memory_mb": 128,
                 "writes": 837, "log_bytes": calls * 600}
        return {"run_id": f"r{window}", "mapping": {"window_s": window, "batch_size": batch}, "sent_messages": 2515,
                "run_hours": 1.9, "calls": {"calls": calls, "mean_batch": 2515 / calls, "median_batch": 1,
                                            "mean_ms": 40, "median_ms": 38, "billed_ms": calls * 40},
                "p50_s": 1.0, "p95_s": 2.0, "stored": 837, "completions": 837, "dlq_messages": 0,
                "sqs_by_action": {"ReceiveMessage": 2600, "DeleteMessageBatch": 2400}, "sqs_requests": 5000,
                "sqs_metrics": {"NumberOfEmptyReceives": 100}, "log_bytes": calls * 600,
                "sqs_requests_basis": "counted (CloudTrail data events)", "cost": latency.costs(units, 1.9)}
    idle = {**latency.idle_cost(1800, 0.5), "by_action": {"ReceiveMessage": 1790}, "empty_receives": 1790,
            "start": "2026-11-23T08:33:00+00:00"}
    text = latency.table([measured(0, 10, 2400), measured(30, 100, 200)], [idle, idle])
    assert "idle: requests per hour" in text and "traffic part: load minus idle, per hour" in text
    first = text.splitlines()[0]
    assert "0 s, batch 10" in first and "30 s, batch 100" in first
    assert "messages in the dead-letter queue" in text and "% of the 30 USD lease" in text
    import re
    assert not re.search(r"\b\d{12}\b", text)                          # no account number of any kind


def test_a_run_without_an_idle_measurement_reads_not_measured():
    run = {"run_id": "r60", "mapping": {"window_s": 60, "batch_size": 100}, "sent_messages": 2515, "run_hours": 1.9,
           "calls": {"calls": 100, "mean_batch": 25.15, "median_batch": 20, "mean_ms": 90, "median_ms": 80,
                     "billed_ms": 9000}, "p50_s": 40.0, "p95_s": 64.0, "stored": 837, "completions": 837,
           "dlq_messages": 0, "sqs_by_action": {"ReceiveMessage": 9000, "DeleteMessageBatch": 260},
           "sqs_requests": 12000, "sqs_metrics": {"NumberOfEmptyReceives": 6500}, "log_bytes": 60000,
           "sqs_requests_basis": "counted (CloudTrail data events)",
           "cost": latency.costs({"sqs_requests": 12000, "invocations": 100, "billed_ms": 9000, "memory_mb": 128,
                                  "writes": 837, "log_bytes": 60000}, 1.9)}
    idle = {**latency.idle_cost(192, 0.5), "by_action": {"ReceiveMessage": 180}, "empty_receives": 180,
            "start": "2026-11-23T08:33:00+00:00"}
    lines = latency.table([run, run], [idle, None]).splitlines()
    row = next(x for x in lines if x.startswith("idle: requests per hour"))
    assert row.split()[-2:] == ["not", "measured"] and "384" in row
    assert len({len(x) for x in lines[:lines.index("")]}) == 1           # every row as wide as the others


def test_trail_counts_that_miss_sends_are_not_used():
    counted = latency.queue_requests({"q SendMessageBatch": 1189, "q ReceiveMessage": 1018}, "q")
    warned = []
    assert latency.usable(counted, 2399, warn=warned.append) is None and "incomplete" in warned[0]
    assert latency.usable(counted, 1189) == counted


def test_a_schedule_group_being_deleted_is_reported_absent():
    """Seen live: after `run.py stop` the group stays DELETING for a while, and the dry run said it would remove it."""
    class Sched:
        class exceptions:
            ResourceNotFoundException = KeyError
        deleted = []

        def get_schedule_group(self, Name):
            return {"Name": Name, "State": "DELETING"}

        def delete_schedule_group(self, Name):
            self.deleted.append(Name)

    class Session:
        region_name = REGION

        def __init__(self):
            self.real = boto3.Session(region_name=REGION)

        def client(self, name):
            return Sched() if name == "scheduler" else self.real.client(name)

    with mock_aws():
        steps = clean_up.clean(Session(), "recipe-test")
    assert "schedule group recipe-test-source-poller (already being deleted)" in steps.absent
    assert Sched.deleted == [] and steps.failed == []


def test_the_trail_is_counted_over_the_run_window_only():
    from datetime import datetime, timezone
    seen = []

    def count(session, prefix, start, end):
        seen.append((prefix, start, end))
        return {"q SendMessageBatch": 1}

    start, end = datetime(2026, 11, 23, 5, 52, tzinfo=timezone.utc), datetime(2026, 11, 23, 7, 50, tzinfo=timezone.utc)
    assert latency.trail_counts(None, "recipe-queue", start, end, count=count) == {"q SendMessageBatch": 1}
    assert seen == [("recipe-queue", start, end)]

    def no_trail(*a):
        raise RuntimeError("NoSuchBucket")
    assert latency.trail_counts(None, "recipe-queue", start, end, count=no_trail) is None


def test_idle_requests_scale_to_30_days_and_split_the_load():
    idle = latency.idle_cost(requests=1800, hours=0.5)              # 3,600 an hour with no message sent
    assert idle["per_hour"] == 3600
    assert idle["month_usd"] == pytest.approx(3600 * 720 * 0.40 / 1e6)
    assert idle["year_cad"] == pytest.approx(3600 * 8760 * 0.40 / 1e6 * 1.45)
    got = latency.split({"sqs_requests": 12000, "run_hours": 2.0}, idle)
    assert got["load_per_hour"] == 6000 and got["traffic_per_hour"] == 2400
    assert got["idle_share"] == pytest.approx(0.6)
