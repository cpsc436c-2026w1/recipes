import json
import os
import time

import pytest

import handler
import operational
from skeleton.config import names
from skeleton.events import distinct_in_window, generate
from skeleton.number import window


@pytest.fixture
def api(aws):
    """The handler over a moto table, called the way a function URL calls it."""
    os.environ["TABLE"] = "t-updates"
    handler._client = aws.client("dynamodb")
    operational.create_table(handler._client, "t-updates")
    yield operational.invoke
    handler._client = None


def ev(i, t, key="st-01", value=5):
    return {"event_id": f"ev-{i}", "entity_id": key, "event_time": t, "value": value}


def test_a_resent_copy_is_stored_once(api):
    first = api("POST", "/updates", {"updates": [ev(1, "2026-11-12T15:30:00.000Z")]})
    again = api("POST", "/updates", {"updates": [ev(1, "2026-11-12T15:30:00.000Z")]})
    assert (first["stored"], again["duplicates"]) == (1, 1)
    assert api("GET", "/number", params={"as_of": "2026-11-12T16:00:10.000Z"})["updates_last_hour"] == 1


def test_an_older_update_is_counted_but_does_not_replace_the_state(api):
    api("POST", "/updates", {"updates": [ev(2, "2026-11-12T15:40:00.000Z", value=9)]})
    late = api("POST", "/updates", {"updates": [ev(1, "2026-11-12T15:20:00.000Z", value=3)]})
    assert late["older"] == 1
    state = api("GET", "/current", params={"entity_id": "st-01"})
    assert (state["value"], state["last_event_id"]) == (9, "ev-2")
    assert api("GET", "/number", params={"as_of": "2026-11-12T16:00:00.000Z"})["updates_last_hour"] == 2


def test_the_window_is_whole_minutes(api):
    api("POST", "/updates", {"updates": [ev(1, "2026-11-12T14:59:59.999Z"), ev(2, "2026-11-12T15:00:00.000Z"),
                                         ev(3, "2026-11-12T15:59:59.999Z"), ev(4, "2026-11-12T16:00:00.000Z")]})
    r = api("GET", "/number", params={"as_of": "2026-11-12T16:00:45.000Z"})
    assert r["updates_last_hour"] == 2
    assert (r["window_start"], r["window_end"]) == ("2026-11-12T15:00:00.000Z", "2026-11-12T16:00:00.000Z")


@pytest.mark.parametrize("bad", [
    {"updates": []},
    {"updates": [{"event_id": "x"}]},
    {"updates": [ev(1, "2026-11-12 15:30")]},
    {"updates": [ev(1, "2026-11-12T15:30:00.000Z", value="5")]},
    {"updates": [ev(i, "2026-11-12T15:30:00.000Z") for i in range(101)]},
])
def test_bad_requests_store_nothing(api, bad):
    assert api("POST", "/updates", bad)["status"] == 400
    assert api("GET", "/number", params={"as_of": "2026-11-12T16:00:00.000Z"})["updates_last_hour"] == 0


def test_api_and_lake_agree_with_plain_python(aws, local_root, as_of):
    events = generate(150, now=as_of, duplicates=5)
    r = operational.run_local(events, local_root, as_of, table="t-local")
    expected = distinct_in_window(events, *window(as_of))
    assert r["sent"]["duplicates"] == 5
    assert r["api"]["updates_last_hour"] == expected
    assert r["lake"] == expected
    handler._client = None


# ---------------------------------------------------------------- AWS path under moto --------------------------------

def test_setup_deploys_a_signed_url_over_the_table(aws):
    n = names("o1", account="123456789012")
    url = operational.setup(aws, n, "123456789012", sleep=lambda s: None)
    assert url.startswith("https://")
    lam = aws.client("lambda")
    assert lam.get_function_url_config(FunctionName="o1-api")["AuthType"] == "AWS_IAM"
    cfg = lam.get_function_configuration(FunctionName="o1-api")
    assert cfg["Environment"]["Variables"]["TABLE"] == "o1-updates" and cfg["Architectures"] == ["arm64"]
    ddb = aws.client("dynamodb")
    pitr = ddb.describe_continuous_backups(TableName="o1-updates")["ContinuousBackupsDescription"]
    assert pitr["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] == "ENABLED"
    assert ddb.describe_time_to_live(TableName="o1-updates")["TimeToLiveDescription"]["AttributeName"] == "expires_at"
    assert ddb.describe_table(TableName="o1-updates")["Table"]["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"


def test_function_role_reaches_only_the_table_and_its_logs(aws):
    n = names("o2", account="123456789012")
    operational.setup(aws, n, "123456789012", sleep=lambda s: None)
    doc = aws.client("iam").get_role_policy(RoleName="o2-api-role", PolicyName="least-privilege")["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    actions = {a for s in doc["Statement"] for a in s["Action"]}
    assert not any(a.endswith("*") or a in ("dynamodb:Scan", "dynamodb:DeleteTable") for a in actions)
    assert all("o2" in s["Resource"] for s in doc["Statement"])


def test_export_reaches_the_bucket(aws):
    n = names("o3", account="123456789012")
    operational.setup(aws, n, "123456789012", sleep=lambda s: None)
    folder = operational.export(aws, n, "123456789012", timeout_s=30, sleep=lambda s: time.sleep(0.05))
    assert folder.startswith("s3://o3-lake-123456789012/exports/AWSDynamoDB/") and folder.endswith("/data/")
