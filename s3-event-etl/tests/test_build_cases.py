"""The design's build tests notification-merge, hang-bounded and glue-run-priced, and the defects the live run of
2026-11-23 found (each test names it), offline: moto, with Glue's StartJobRun and GetJobRun answered by a stub."""
import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from botocore.exceptions import ClientError

import athena_load
import check
import control
import etl_job
import sweep

AT = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
GOOD = {"delivery_id": "dl-1", "event_type": "trip_requested", "event_id": "ev-1", "trip_id": "t-1",
        "pickup_zone_id": 48, "event_time": "2026-11-23T09:05:10Z", "received_time": "2026-11-23T09:05:11Z"}


@pytest.fixture
def lake(aws, monkeypatch, tmp_path):
    import lake as lake_mod
    import run_log
    for k in ("ENGINE", "ETL_MIN_GAP_MIN", "MAX_RUNS_PER_DAY", "ETL_FAULT", "ETL_FAULT_TIMEOUT_MIN", "START_ON_STAGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PREFIX", "recipe")
    monkeypatch.setenv("ETL_LOCAL_DB", str(tmp_path / "lake.duckdb"))
    lake_mod.up(aws, log=lambda *a: None)
    run_log.ensure_run_log(aws)
    aws.client("sns").create_topic(Name="recipe-etl-alerts")
    return aws, lake_mod.bucket_name(aws)


class Session:
    """The moto session, with Glue's job runs and SNS's publish answered here and written down."""

    def __init__(self, real, states):
        self.real, self.states, self.started, self.published = real, list(states), [], []

    def client(self, name, **kw):
        c = self.real.client(name, **kw)
        if name == "glue":
            outer = self

            class Glue:
                exceptions = c.exceptions

                def start_job_run(self, **kw):
                    outer.started.append(kw)
                    return {"JobRunId": f"jr_{len(outer.started)}"}

                def get_job_run(self, JobName, RunId):
                    n = int(RunId.split("_")[1])
                    return {"JobRun": {"Id": RunId, "JobRunState": outer.states[n - 1]}}
            return Glue()
        if name == "sns":
            outer = self

            class Sns:
                def publish(self, **kw):
                    outer.published.append(kw)
                    return {"MessageId": str(len(outer.published))}
            return Sns()
        return c


def stage_one(sess, bucket, name="a"):
    sess.client("s3").put_object(Bucket=bucket, Key=f"stage/trip-events/dt=2026-11-23/{name}-1.jsonl",
                                 Body=(json.dumps(GOOD) + "\n").encode())


# ---- hang-bounded [R60, R70]

def test_hang_bounded_three_timeouts_hold_the_job_until_resume(lake, monkeypatch):
    """Three runs that end TIMEOUT are each recorded failed with one email; then no run starts, the hold is recorded
    once as skipped, and after etl.py resume a run starts (the live run did it with a 1-minute timeout)."""
    real, bucket = lake
    monkeypatch.setenv("ETL_MIN_GAP_MIN", "0")
    monkeypatch.setenv("MAX_RUNS_PER_DAY", "10")
    s = Session(real, ["TIMEOUT", "TIMEOUT", "TIMEOUT", "SUCCEEDED"])
    stage_one(real, bucket)
    for i in range(3):
        at = AT + timedelta(minutes=20 * i)
        assert control.maybe_start(bucket, s, at, log=lambda *a: None)
        sweep.follow_runs(s, at + timedelta(minutes=12), log=lambda *a: None)
    items = control.runs(control.ddb(real))
    assert [control._s(i, "run_state") for i in items] == ["failed"] * 3
    assert all(control._s(i, "error_text") == "TIMEOUT" for i in items)
    assert len(s.published) == 3 and all("failed" in p["Subject"] for p in s.published)
    out = []
    assert control.maybe_start(bucket, s, AT + timedelta(hours=2), log=out.append) is None
    assert control.maybe_start(bucket, s, AT + timedelta(hours=3), log=out.append) is None
    held = [i for i in control.runs(control.ddb(real)) if control._s(i, "error_text") == control.HELD]
    assert len(held) == 1 and control._s(held[0], "run_state") == "skipped" and out == [f"no start: {control.HELD}"] * 2
    monkeypatch.setattr(control, "now", lambda: AT + timedelta(hours=4))
    control.resume(real, log=lambda *a: None)
    assert control.maybe_start(bucket, s, AT + timedelta(hours=4, minutes=1), log=lambda *a: None)
    assert len(s.started) == 4


def test_a_run_still_running_past_its_timeout_and_5_minutes_is_failed(lake, monkeypatch):
    real, bucket = lake
    s = Session(real, ["RUNNING"])
    stage_one(real, bucket)
    assert control.maybe_start(bucket, s, AT, log=lambda *a: None)
    sweep.follow_runs(s, AT + timedelta(minutes=14), log=lambda *a: None)
    assert control._s(control.runs(control.ddb(real))[0], "run_state") == "running"
    sweep.follow_runs(s, AT + timedelta(minutes=control.GLUE_TIMEOUT_MIN + 6), log=lambda *a: None)
    assert control._s(control.runs(control.ddb(real))[0], "run_state") == "failed" and len(s.published) == 1


def test_the_job_is_created_with_a_10_minute_timeout_and_a_fault_only_shortens_one_run(lake, monkeypatch):
    real, bucket = lake
    s = Session(real, [])
    stage_one(real, bucket)
    monkeypatch.setenv("ETL_FAULT", "hang")
    monkeypatch.setenv("ETL_FAULT_TIMEOUT_MIN", "1")
    assert control.maybe_start(bucket, s, AT, log=lambda *a: None)
    assert s.started[0]["Arguments"]["--HANG"] == "true" and s.started[0]["Timeout"] == 1
    monkeypatch.setenv("ETL_FAULT", "nonsense")
    with pytest.raises(ValueError, match="ETL_FAULT is one of"):
        control.fault()
    monkeypatch.delenv("ETL_FAULT")
    assert control.fault() == ({}, {})


# ---- glue-run-priced [R70]

def test_glue_run_priced_from_the_measured_run():
    """The live run of SIM01 on 2026-11-23: ExecutionTime 95 s, DPUSeconds 191 -> 0.0233 USD at 0.44 a DPU-hour.
    Under 180 s, so the Cost section's 90 s assumption is replaced by the measured 191 DPU-seconds, not repriced up."""
    import etl
    c = etl.glue_run_cost({"ExecutionTime": 95, "DPUSeconds": 191.0, "NumberOfWorkers": 2, "WorkerType": "G.1X"})
    assert c["how"] == "DPUSeconds" and round(c["usd"], 4) == 0.0233 and round(c["cad"], 4) == 0.0338
    short = etl.glue_run_cost({"ExecutionTime": 20, "NumberOfWorkers": 2, "WorkerType": "G.1X"})
    assert short["dpu_s"] == 120 and short["how"] == "ExecutionTime x DPU"        # the 1-minute minimum


def test_runs_report_prints_each_run_and_the_total(lake, monkeypatch):
    import etl
    real, _ = lake

    class Glue:
        def get_job_runs(self, **kw):
            return {"JobRuns": [{"Id": "jr_aaaaaaaaaaaa1", "JobRunState": "SUCCEEDED", "ExecutionTime": 95,
                                 "DPUSeconds": 191.0}, {"Id": "jr_bbbbbbbbbbbb2", "JobRunState": "TIMEOUT",
                                                        "ExecutionTime": 70, "DPUSeconds": 140.0}]}

    class S:
        def client(self, name):
            return Glue()
    out = []
    total = etl.runs_report(S(), log=out.append)
    assert round(total, 4) == round(331 / 3600 * 0.44, 4) and out[-1].startswith("total    0.0405 USD")


# ---- notification-merge [R90]

def test_notification_merge_keeps_another_recipes_entry_through_up_and_clean_up(lake, monkeypatch):
    import clean_up
    import etl
    real, bucket = lake
    s3 = real.client("s3")
    real.client("sqs").create_queue(QueueName="quality-gate")
    other = {"Id": "quality-gate", "QueueArn": "arn:aws:sqs:ca-central-1:123456789012:quality-gate",
             "Events": ["s3:ObjectCreated:*"], "Filter": {"Key": {"FilterRules": [
                 {"Name": "prefix", "Value": "raw/orders/"}]}}}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [other]})
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    assert [e["Id"] for e in config["QueueConfigurations"]] == ["quality-gate"]
    assert [e["Id"] for e in config["LambdaFunctionConfigurations"]] == ["recipe-s3-event-etl"]
    assert clean_up.clean(real, yes=True, log=lambda *a: None) == 0
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    assert "LambdaFunctionConfigurations" not in config
    assert [e["Id"] for e in config["QueueConfigurations"]] == ["quality-gate"]


def test_notification_merge_an_overlapping_entry_stops_up_naming_its_id():
    """S3 refuses two entries whose prefixes and suffixes overlap for one event type (moto does not): the error
    names the other entry's Id."""
    import etl

    class S3:
        def get_bucket_notification_configuration(self, Bucket):
            return {"LambdaFunctionConfigurations": [{"Id": "a3-quality-gate", "LambdaFunctionArn": "arn:x",
                                                      "Events": ["s3:ObjectCreated:*"]}]}

        def put_bucket_notification_configuration(self, **kw):
            raise ClientError({"Error": {"Code": "InvalidArgument", "Message": "Configurations overlap. "
                               "Configurations on the same bucket cannot share a common event type."}},
                              "PutBucketNotificationConfiguration")
    with pytest.raises(SystemExit, match="overlap another entry's \\(a3-quality-gate\\)"):
        etl.add_entry(S3(), "b", {"Id": "recipe-s3-event-etl", "LambdaFunctionArn": "arn:y",
                                  "Events": ["s3:ObjectCreated:*"]})


# ---- defects of the live run, 2026-11-23

def test_source_key_is_the_bare_key_on_the_athena_path_too():
    """The Glue job and the offline load write source_key without s3://<bucket>/; the Athena path wrote "$path"."""
    expr = athena_load.source_key_sql("recipe-lake-123456789012")
    sql = athena_load.source_sql("recipe_lake", "recipe-lake-123456789012", {"2026-11-23": ["stage/x.jsonl"]})
    assert f"{expr} AS source_key" in sql and '"$path" AS source_key' not in sql
    got = duckdb.sql("SELECT " + expr.replace('"$path"', "'s3://recipe-lake-123456789012/stage/trip-events/dt=2026-11-23/a.jsonl'"))
    assert got.fetchall()[0][0] == "stage/trip-events/dt=2026-11-23/a.jsonl"


def test_with_athena_the_check_function_does_not_start_a_load(lake, monkeypatch):
    """Live: the check's role has no Athena actions, and its start was recorded as a failed run."""
    real, bucket = lake
    monkeypatch.setenv("ENGINE", "athena")
    key = "raw/trip-events/dt=2026-11-23/0905-a.jsonl"
    real.client("s3").put_object(Bucket=bucket, Key=key, Body=(json.dumps(GOOD) + "\n").encode())
    assert check.check_object(bucket, key, "0A1B", real, start=True, log=lambda *a: None)["good"] == 1
    assert control.runs(control.ddb(real)) == []


def test_up_attaches_no_managed_policy_and_the_glue_role_names_its_catalog_entries(lake):
    """Live: iam:AttachRolePolicy was refused where CreateRole and PutRolePolicy were allowed."""
    import etl
    real, _ = lake
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    iam = real.client("iam")
    for role in etl.names()["roles"].values():
        assert iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"] == []
    doc = iam.get_role_policy(RoleName="recipe-etl-glue-role", PolicyName="s3-event-etl")["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    glue = next(s for s in doc["Statement"] if "glue:UpdateTable" in s["Action"])
    assert all("recipe_lake" in r or r.endswith(":catalog") for r in glue["Resource"])
    sweep_doc = iam.get_role_policy(RoleName="recipe-etl-sweep-role", PolicyName="s3-event-etl")["PolicyDocument"]
    sweep_doc = json.loads(sweep_doc) if isinstance(sweep_doc, str) else sweep_doc
    assert any("s3:GetBucketLocation" in s["Action"] for s in sweep_doc["Statement"])      # "Unable to verify ... bucket"


def test_a_second_up_with_another_engine_reaches_the_functions_environment(lake, monkeypatch):
    import etl
    real, _ = lake
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    monkeypatch.setenv("ENGINE", "athena")
    monkeypatch.setenv("ETL_MIN_GAP_MIN", "30")
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    env = real.client("lambda").get_function_configuration(FunctionName="recipe-etl-sweep")["Environment"]["Variables"]
    assert env["ENGINE"] == "athena" and env["ETL_MIN_GAP_MIN"] == "30"


def test_with_athena_up_creates_the_iceberg_table(lake, monkeypatch):
    """Live: nothing made trip_events_clean without a Glue run, and the first MERGE found no table."""
    import etl
    import tags
    real, _ = lake
    monkeypatch.setenv("ENGINE", "athena")
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    assert tags.recorded("glue-table", "recipe_lake.trip_events_clean") == "created"
    ddl = etl_job.athena_table_ddl("recipe_lake", "b")
    assert "'table_type' = 'ICEBERG'" in ddl and "PARTITIONED BY (event_day)" in ddl
    assert "LOCATION 's3://b/lake/trip_events_clean/'" in ddl


def test_clean_up_records_every_delete_in_the_ledger(lake, monkeypatch):
    """Live: the teardown found ledger rows still `created` for tables and roles this clean-up had deleted."""
    import clean_up
    import etl
    import tags
    real, _ = lake
    monkeypatch.setenv("ENGINE", "athena")
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    made = {(r["type"], r["name"]) for r in tags.ledger_rows() if r["state"] == "created"}
    assert clean_up.clean(real, yes=True, log=lambda *a: None) == 0
    still = {k for k in made if tags.recorded(*k) == "created"}
    # kept on purpose: the bucket and workgroup (lake.py down) and the database (other recipes' tables)
    assert {k for k, _ in still} <= {"s3-bucket", "athena-workgroup", "glue-database"}, still


def test_up_waits_until_a_function_reads_its_environment():
    """Live: a function made seconds after its new role failed every call with KMSAccessDeniedException, and S3's
    events of one landing were all lost; up now invokes each function and writes its environment again until it
    answers."""
    import io
    import etl

    class Lam:
        def __init__(self):
            self.calls, self.updates, self.broken = 0, 0, False

        def invoke(self, FunctionName, Payload):
            self.calls += 1
            if self.calls == 1 or self.broken:
                raise ClientError({"Error": {"Code": "KMSAccessDeniedException", "Message": "unable to decrypt"}},
                                  "Invoke")
            if self.calls == 2:
                return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorMessage": "x"}')}
            return {"Payload": io.BytesIO(b'{"ready": true}')}

        def update_function_configuration(self, **kw):
            self.updates += 1

        def get_waiter(self, name):
            return type("W", (), {"wait": lambda self, **kw: None})()

    lam = Lam()
    s = type("S", (), {"client": lambda self, name: lam})()
    out = []
    etl.wait_ready(s, "recipe-etl-check", {"PREFIX": "recipe"}, sleep=lambda x: None, log=out.append)
    assert lam.calls == 3 and lam.updates == 2 and "KMSAccessDeniedException" in out[0]
    lam.broken = True                                  # never answers
    with pytest.raises(SystemExit, match="does not answer after 2 tries"):
        etl.wait_ready(s, "recipe-etl-check", {}, tries=2, sleep=lambda x: None, log=out.append)


def test_both_handlers_answer_ready_without_work():
    assert check.handler({"ready": True}) == {"ready": True}
    assert sweep.handler({"ready": True}) == {"ready": True}


def test_clean_up_says_already_gone_and_keeps_the_ledger_right(lake, monkeypatch):
    """Glue DeleteJob and SNS DeleteTopic succeed on a missing name, so a second clean-up printed "removed" for
    what the first had deleted (live, 2026-11-23). Now each resource is asked for first."""
    import clean_up
    import etl
    import tags
    real, _ = lake
    etl.up(real, deps=False, log=lambda *a: None, sleep=lambda s: None)
    real.client("glue").delete_job(JobName="recipe-etl")             # gone before the clean-up, ledger `created`
    dry = []
    assert clean_up.clean(real, yes=False, log=dry.append) == 0
    assert "already gone Glue job recipe-etl" in dry and "would remove function recipe-etl-check" in dry
    assert tags.recorded("glue-job", "recipe-etl") == "created"       # a dry run writes no ledger row
    first = []
    assert clean_up.clean(real, yes=True, log=first.append) == 0
    assert "already gone Glue job recipe-etl" in first and "removed  topic recipe-etl-alerts" in first
    assert tags.recorded("glue-job", "recipe-etl") == "deleted"
    again = []
    assert clean_up.clean(real, yes=True, log=again.append) == 0
    assert not [ln for ln in again if ln.startswith("removed")], again
    assert "already gone topic recipe-etl-alerts" in again and "already gone Glue job recipe-etl" in again
