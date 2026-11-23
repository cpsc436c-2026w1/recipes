"""live_check.py and check.py: the cron arithmetic, the verdict's words, the function's policy, and check.py up
and alarm in moto (Lambda settings, retries 0, the tagged schedule group)."""
import json
from datetime import date

import pytest

import check
import live_check


@pytest.mark.parametrize("expr, day, due", [
    ("cron(0 6 * * ? *)", date(2026, 11, 24), True),
    ("cron(0 6 ? * MON *)", date(2026, 11, 30), True),        # a Monday
    ("cron(0 6 ? * MON *)", date(2026, 11, 24), False),
    ("cron(0 6 ? * 2 *)", date(2026, 11, 30), True),          # AWS: 1 = Sunday, 2 = Monday
    ("cron(0 6 ? * MON-FRI *)", date(2026, 11, 29), False),   # a Sunday
    ("cron(0 6 1,15 * ? *)", date(2026, 12, 15), True),
    ("cron(0 6 * DEC ? 2026)", date(2026, 11, 24), False)])
def test_due_on(expr, day, due):
    assert live_check.due_on(expr, day) is due


def test_read_cron_adds_the_delay_and_refuses_midnight():
    assert live_check.read_cron("cron(0 6 * * ? *)", 90) == "cron(30 7 * * ? *)"
    assert live_check.read_cron("cron(45 22 ? * MON *)", 60) == "cron(45 23 ? * MON *)"
    with pytest.raises(ValueError, match="past midnight"):
        live_check.read_cron("cron(0 23 * * ? *)", 90)
    with pytest.raises(ValueError):
        live_check.read_cron("cron(0/15 * * * ? *)", 90)


def test_verdict_words():
    d = date(2026, 11, 24)
    assert live_check.verdict_message(d, None) == "2026-11-24: no check"
    assert live_check.verdict_message(d, {"run_state": "finished", "checked_count": 20}) is None
    assert live_check.verdict_message(d, {"run_state": "failed", "checked_count": 19, "error_text": ""}) == \
        "2026-11-24: 19 probe trips, not 20"
    assert live_check.verdict_message(d, {"run_state": "failed", "checked_count": -1, "error_text": "boom"}) == \
        "2026-11-24: boom"


def test_the_function_policy_names_only_its_own_resources(monkeypatch):
    monkeypatch.setenv("CHECK_TARGET", "kinesis:recipe-test-trip-events")
    monkeypatch.setenv("CHECK_QUERY", "dynamodb:recipe-test-sim-events")
    policy = check.function_policy(check.names(), "111122223333", check.settings())
    by_action = {a: s["Resource"] for s in policy["Statement"] for a in s["Action"]}
    assert by_action["dynamodb:PutItem"] == by_action["dynamodb:GetItem"] == \
        "arn:aws:dynamodb:ca-central-1:111122223333:table/recipe-test-runs"
    assert by_action["sns:Publish"] == "arn:aws:sns:ca-central-1:111122223333:recipe-test-check-alerts"
    assert by_action["kinesis:PutRecords"].endswith(":stream/recipe-test-trip-events")
    assert by_action["dynamodb:Scan"].endswith(":table/recipe-test-sim-events")
    assert all("*" not in str(r) for a, r in by_action.items() if a.startswith(("dynamodb", "sns", "kinesis")))


def test_up_in_moto(aws, monkeypatch):
    monkeypatch.setenv("CHECK_TARGET", "s3:recipe-test-lake-123456789012")
    monkeypatch.setenv("CHECK_QUERY", "s3:recipe-test-lake-123456789012")
    sent = []
    aws.events.register("before-parameter-build.lambda.CreateFunction", lambda params, **_: sent.append(params))
    check.up(aws, log=lambda *_: None, sleep=lambda s: None)
    lam = aws.client("lambda")
    cfg = lam.get_function(FunctionName="recipe-test-live-check")["Configuration"]
    assert (cfg["Runtime"], cfg["Architectures"], cfg["MemorySize"], cfg["Timeout"]) == ("python3.12", ["arm64"], 256, 120)
    assert lam.get_function_event_invoke_config(FunctionName="recipe-test-live-check")["MaximumRetryAttempts"] == 0
    assert sent[0]["Tags"]["Project"] == "436c"
    sched = aws.client("scheduler")
    group = sched.get_schedule_group(Name="recipe-test-simulated-runs")
    assert {t["Key"] for t in sched.list_tags_for_resource(ResourceArn=group["Arn"])["Tags"]} >= {"Project", "Team"}
    got = {s["Name"]: sched.get_schedule(Name=s["Name"], GroupName="recipe-test-simulated-runs")
           for s in sched.list_schedules(GroupName="recipe-test-simulated-runs")["Schedules"]}
    assert sorted(got) == ["recipe-test-check-read", "recipe-test-check-send", "recipe-test-check-verdict"]
    assert got["recipe-test-check-verdict"]["ScheduleExpression"] == "cron(15 0 * * ? *)"
    assert all(g["Target"]["RetryPolicy"]["MaximumRetryAttempts"] == 0 for g in got.values())
    assert json.loads(got["recipe-test-check-send"]["Target"]["Input"]) == {"phase": "send"}
    assert aws.client("logs").describe_log_groups(logGroupNamePrefix="/aws/lambda/recipe-test-live-check")[
        "logGroups"][0]["retentionInDays"] == 7


def test_the_package_carries_what_the_function_needs():
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(check.package(['{"a": 1}'])))
    assert sorted(z.namelist()) == ["live_check.py", "probe.jsonl", "replay.py"]
    assert "from rides" not in z.read("replay.py").decode().split("def load_deliveries")[0]


def _refusing(aws, service, method):
    """The session, with one call of one client refused as a lease's sign-in may refuse it."""
    from botocore.exceptions import ClientError
    make = aws.client

    def client(name, *a, **kw):
        c = make(name, *a, **kw)
        if name == service:
            def refuse(**_):
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}}, method)
            setattr(c, method, refuse)
        return c
    aws.client = client
    return aws


def test_a_refused_tagged_group_puts_the_schedules_in_default_and_clean_up_finds_them(aws, monkeypatch):
    import clean_up
    monkeypatch.setenv("CHECK_TARGET", "kinesis:recipe-test-trip-events")
    monkeypatch.setenv("CHECK_QUERY", "dynamodb:recipe-test-sim-events")
    out = []
    check.up(_refusing(aws, "scheduler", "create_schedule_group"), log=out.append, sleep=lambda s: None)
    assert any("REFUSED  CreateScheduleGroup" in line for line in out)
    names = sorted(x["Name"] for x in aws.client("scheduler").list_schedules(GroupName="default")["Schedules"])
    assert names == ["recipe-test-check-read", "recipe-test-check-send", "recipe-test-check-verdict"]
    lines = []
    assert not clean_up.clean(aws, yes=True, log=lines.append).failed
    assert sum("removed  schedule recipe-test-check-" in line for line in lines) == 3
    assert not aws.client("scheduler").list_schedules(GroupName="default")["Schedules"]


def test_refused_retries_setting_is_reported_and_up_goes_on(aws, monkeypatch):
    monkeypatch.setenv("CHECK_TARGET", "kinesis:recipe-test-trip-events")
    monkeypatch.setenv("CHECK_QUERY", "dynamodb:recipe-test-sim-events")
    out = []
    result = check.up(_refusing(aws, "lambda", "put_function_event_invoke_config"), log=out.append,
                      sleep=lambda s: None)
    assert any("retries stay at 2" in line for line in out) and len(result["schedules"]) == 3
