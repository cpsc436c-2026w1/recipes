"""clean_up.py in moto: the dry run deletes nothing, --yes deletes what the ledger created, keeps what it
borrowed, and --check finds nothing left after lake.py down."""
import clean_up
import check
import lake
import live_run
import run_log
import tags


def test_clean_up_dry_run_then_yes_then_check(aws, monkeypatch):
    monkeypatch.setenv("CHECK_TARGET", "kinesis:recipe-test-trip-events")
    monkeypatch.setenv("CHECK_QUERY", "dynamodb:recipe-test-sim-events")
    lake.up(aws, log=lambda *_: None)
    lake.ensure_workgroup("recipe-test-wg", sess=aws)
    live_run.stream_up(aws, log=lambda *_: None)
    check.up(aws, log=lambda *_: None, sleep=lambda s: None)
    check.alarm(aws, log=lambda *_: None)
    aws.client("s3").put_object(Bucket="recipe-test-lake-123456789012",
                                Key="raw/trip-events/dt=2026-11-23/20261123T0900Z-r.jsonl", Body=b"{}\n")
    out = []
    steps = clean_up.clean(aws, yes=False, log=out.append)
    assert not steps.failed and any(line.startswith("would remove") for line in out)
    assert aws.client("lambda").list_functions()["Functions"]               # nothing deleted yet
    out = []
    steps = clean_up.clean(aws, yes=True, log=out.append)
    assert not steps.failed, steps.failed
    text = "\n".join(out)
    for what in ("Kinesis stream recipe-test-trip-events", "schedule group recipe-test-simulated-runs",
                 "alarm recipe-test-check-failed", "Lambda function recipe-test-live-check",
                 "IAM role recipe-test-check-role", "DynamoDB table recipe-test-runs",
                 "SNS topic recipe-test-check-alerts"):
        assert f"removed  {what}" in text, what
    # the file of run "r" is not this ledger's: lake.py down refuses the bucket while it is there
    assert lake.down(aws, yes=True, log=lambda *_: None) == 1
    tags.record("s3-replay", "r", "created", script="test")       # as live_run.py run --to s3 records it
    assert lake.down(aws, yes=True, log=lambda *_: None) == 0
    assert clean_up.check(aws, log=lambda *_: None) == 0
    assert tags.recorded("iam-role", "recipe-test-check-role") == "deleted"
    again = clean_up.clean(aws, yes=True, log=lambda *_: None)
    assert not again.failed and not again.removed                            # a second run is safe


def test_a_borrowed_run_log_is_kept(aws):
    aws.client("dynamodb").create_table(
        TableName="recipe-test-runs", KeySchema=run_log.KEY_SCHEMA, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "job", "AttributeType": "S"},
                              {"AttributeName": "run_id", "AttributeType": "S"}])
    run_log.ensure_run_log(aws)
    out = []
    clean_up.clean(aws, yes=True, log=out.append)
    assert "recipe-test-runs" in aws.client("dynamodb").list_tables()["TableNames"]
    assert any("kept: borrowed" in line for line in out)


def test_clean_up_deletes_only_this_recipes_files_under_raw_trip_events(aws, monkeypatch, tmp_path):
    """Other recipes replay into raw/trip-events/ of the same bucket: --yes deletes the files of this recipe's run
    ids (the ledger's s3-replay rows, the folders of evidence/) and the probe's, and keeps every other file."""
    monkeypatch.setattr(clean_up, "EVIDENCE", tmp_path / "evidence")
    (tmp_path / "evidence" / "local-try").mkdir(parents=True)
    lake.up(aws, log=lambda *_: None)
    bucket = lake.bucket_name(aws)
    tags.record("s3-replay", "mine-s3", "created", script="test")
    tags.record("lambda-function", "recipe-test-live-check", "created", script="test")
    s3 = aws.client("s3")
    day = "raw/trip-events/dt=2026-11-23/20261123T0900Z"
    keys = {f"{day}-mine-s3.jsonl": True, f"{day}-local-try.jsonl": True, f"{day}-probe-2026-11-23.jsonl": True,
            f"{day}-another-recipe-s3.jsonl": False, f"{day}-s3.jsonl": False,
            "raw/trip-events/dt=2026-11-23/hour=09/deliveries.jsonl": False}
    for key in keys:
        s3.put_object(Bucket=bucket, Key=key, Body=b"{}\n")
    clean_up.clean(aws, yes=False, log=lambda *_: None)
    left = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="raw/")["Contents"]}
    assert left == set(keys)                                                 # the bare run deletes nothing
    steps = clean_up.clean(aws, yes=True, log=lambda *_: None)
    assert not steps.failed, steps.failed
    left = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="raw/")["Contents"]}
    assert left == {k for k, mine in keys.items() if not mine}


def test_probe_files_are_kept_when_this_ledger_did_not_deploy_the_check(aws, monkeypatch, tmp_path):
    monkeypatch.setattr(clean_up, "EVIDENCE", tmp_path / "evidence")
    lake.up(aws, log=lambda *_: None)
    bucket = lake.bucket_name(aws)
    key = "raw/trip-events/dt=2026-11-23/20261123T0900Z-probe-2026-11-23.jsonl"
    aws.client("s3").put_object(Bucket=bucket, Key=key, Body=b"{}\n")
    clean_up.clean(aws, yes=True, log=lambda *_: None)
    assert aws.client("s3").head_object(Bucket=bucket, Key=key)
