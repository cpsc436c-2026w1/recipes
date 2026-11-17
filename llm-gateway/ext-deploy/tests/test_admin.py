import json

import boto3
import pytest

from conftest import key_item, run_admin, usage_rows
from gateway import admin, auth, aws, budget, settings


def test_issue_prints_key_once_and_stores_hash(issue):
    text = run_admin("issue", "--owner", "bo", "--kind", "person", "--team", "t07")
    key = text.strip().splitlines()[-1]
    assert auth.KEY_RE.match(key)
    assert text.count(key) == 1
    item = key_item(auth.KEY_RE.match(key).group(1))
    assert item["key_hash"] == auth.hash_key(key)
    assert key not in json.dumps(item, default=str)
    assert item["budget_micro"] == 3_000_000 and item["team"] == "t07"


def test_list_hides_secret(issue):
    key_id, key = issue(owner="cy")
    out = run_admin("list", "--owner", "cy")
    assert key_id in out
    assert key[len(key_id) + 1:] not in out
    assert "3.000000 USD" in out


def test_revoke(issue):
    key_id, _ = issue(owner="di")
    run_admin("revoke", key_id)
    assert key_item(key_id)["state"] == "revoked"
    assert "revoked_at" in key_item(key_id)


def _seed(key_id, day, model, cost, n=1):
    aws.usage_table().update_item(
        Key={"key_id": key_id, "sk": f"{day}#{model}"},
        UpdateExpression="ADD requests :n, tokens_in :n, tokens_out :n, cost_micro :c",
        ExpressionAttributeValues={":n": n, ":c": cost})


def test_spend_per_key_and_team(issue):
    a, _ = issue(owner="ea", team="t99")
    b, _ = issue(owner="fi", team="t99")
    _seed(a, "2026-11-04", "example-large", 1_500_000)
    _seed(b, "2026-11-05", "example-small", 250_000)
    by_key = run_admin("spend", "--by", "key", "--key", a)
    assert f"total {a}  1.500000 USD" in by_key
    by_team = run_admin("spend", "--by", "team", "--team", "t99")
    assert "total t99  1.750000 USD" in by_team
    since = run_admin("spend", "--by", "team", "--team", "t99", "--since", "2026-11-05")
    assert "total t99  0.250000 USD" in since


def test_reconcile_restores_spent(issue):
    key_id, _ = issue(owner="gu")
    _seed(key_id, "2026-11-04", "example-large", 42)
    out = run_admin("spend", "--reconcile")
    assert key_id in out
    assert key_item(key_id)["spent_micro"] == 42


def test_rate_set_one_key_and_all_active(issue):
    a, _ = issue(owner="ha")
    b, _ = issue(owner="ib")
    c, _ = issue(owner="jo")
    run_admin("revoke", c)
    run_admin("rate", "set", a, "90")
    assert key_item(a)["max_rpm"] == 90
    assert key_item(b)["max_rpm"] == settings.DEFAULT_MAX_RPM
    run_admin("rate", "set", "all", "120")  # the table also holds the other tests' keys, so the count is not checked
    assert key_item(a)["max_rpm"] == key_item(b)["max_rpm"] == 120
    assert key_item(c)["max_rpm"] == settings.DEFAULT_MAX_RPM  # a revoked key is left as it was


def test_dump_and_load_put_the_same_rows_back(issue, tmp_path):
    key_id, key = issue(owner="hana", team="t42", models="example-large,example-small")
    _seed(key_id, "2026-11-04", "example-large", 1_500_000)
    run_admin("spend", "--reconcile")
    budget.rate_limit(key_id, 30)  # a per-minute counter row, which a dump leaves out
    before_key, before_usage = key_item(key_id), usage_rows(key_id)
    f = tmp_path / "dump.json"
    out = run_admin("dump", str(f))
    assert "hana" not in out and key not in out
    assert f.stat().st_mode & 0o777 == 0o600
    doc = json.loads(f.read_text())
    assert not [r for r in doc["usage"] if r["sk"]["S"].startswith("min#")]

    aws.keys_table().delete_item(Key={"key_hash": before_key["key_hash"]})
    aws.usage_table().update_item(Key={"key_id": key_id, "sk": "2026-11-04#example-large"},
                                  UpdateExpression="SET cost_micro = :z", ExpressionAttributeValues={":z": 0})
    with pytest.raises(SystemExit, match="already holds keys"):  # the suite's other keys are in the table
        run_admin("load", str(f))
    run_admin("load", str(f), "--overwrite")
    after = key_item(key_id)
    assert after == before_key
    assert isinstance(after["allowed_models"], set) and after["spent_micro"] == 1_500_000
    assert usage_rows(key_id) == before_usage


def test_load_refuses_a_file_that_is_not_a_dump(topic_arn, tmp_path):
    f = tmp_path / "hashes.json"
    f.write_text(json.dumps({"a" * 64: "t01/ana"}))
    with pytest.raises(SystemExit, match="not a gw-admin dump"):
        run_admin("load", str(f), "--overwrite")


def test_budget_set_clears_notified_above(issue):
    key_id, _ = issue(owner="ha")
    item = key_item(key_id)
    aws.keys_table().update_item(Key={"key_hash": item["key_hash"]},
                                 UpdateExpression="SET spent_micro = :s ADD notified :n",
                                 ExpressionAttributeValues={":s": 2_700_000, ":n": {"50", "80"}})
    run_admin("budget", "set", key_id, "4")  # 2.7 of 4 is 67 percent
    item = key_item(key_id)
    assert item["budget_micro"] == 4_000_000
    assert item["notified"] == {"50"}


def test_big_budget_needs_yes(issue):
    with pytest.raises(SystemExit):
        run_admin("issue", "--owner", "x", "--kind", "team", "--budget", "60")
    run_admin("issue", "--owner", "x", "--kind", "team", "--budget", "60", "--yes")


def test_notify_subscribes_with_filter(topic_arn):
    run_admin("notify", "--owner", "ana", "--email", "ana@example.com")
    sns = boto3.client("sns", region_name="ca-central-1")
    [sub] = [s for s in sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
             if s["Endpoint"] == "ana@example.com"]
    attrs = sns.get_subscription_attributes(SubscriptionArn=sub["SubscriptionArn"])["Attributes"]
    assert json.loads(attrs["FilterPolicy"]) == {"owner": ["ana"]}


def test_kill_and_open(monkeypatch):
    calls = []

    class FakeLambda:
        def put_function_concurrency(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(admin.boto3, "client", lambda *a, **k: FakeLambda())
    run_admin("kill")
    run_admin("open")
    assert [c["ReservedConcurrentExecutions"] for c in calls] == [0, 200]


def _hashes_file(tmp_path, owners: dict[str, str]):
    """Write {SHA-256 hex: owner} for made-up keys. Returns the file and {owner: hash}."""
    by_owner = {owner: auth.hash_key(key) for owner, key in owners.items()}
    f = tmp_path / "hashes.json"
    f.write_text(json.dumps({h: owner for owner, h in by_owner.items()}))
    return f, by_owner


def _item(key_hash):
    return aws.keys_table().get_item(Key={"key_hash": key_hash})["Item"]


def test_import_stores_hashes_and_prints_no_owner(topic_arn, tmp_path):
    f, by_owner = _hashes_file(tmp_path, {"t11/ira": "a" * 43, "t11/jo": "b" * 43, "kai": "c" * 43})
    out = run_admin("import", str(f), "--team-sep", "/", "--budget", "2", "--expires", "2026-12-31")
    assert "imported 3 keys (person), budget 2.000000 USD each, 6.000000 USD in all" in out
    assert "ira" not in out and by_owner["kai"] not in out
    ira, kai = _item(by_owner["t11/ira"]), _item(by_owner["kai"])
    assert ira["key_id"] == admin.imported_key_id(by_owner["t11/ira"]) and ira["key_id"].startswith("im-")
    assert ira["team"] == "t11" and ira["owner"] == "t11/ira" and ira["budget_micro"] == 2_000_000
    assert "team" not in kai and kai["created_by"] == "import"
    assert key_item(ira["key_id"])["key_hash"] == by_owner["t11/ira"]  # spend, budget set and revoke find it by id


def test_import_and_issue_take_a_rate(issue, tmp_path):
    # an optimizer or an eval that runs calls in parallel needs more than the default
    f, by_owner = _hashes_file(tmp_path, {"t13/max": "g" * 43, "t13/lin": "h" * 43})
    run_admin("import", str(f), "--rpm", "120")
    assert _item(by_owner["t13/max"])["max_rpm"] == 120
    key_id, _ = issue(owner="lin", rpm=90)
    assert key_item(key_id)["max_rpm"] == 90
    key_id, _ = issue(owner="ned")
    assert key_item(key_id)["max_rpm"] == settings.DEFAULT_MAX_RPM


def test_import_again_keeps_spend(topic_arn, tmp_path):
    f, by_owner = _hashes_file(tmp_path, {"lu": "d" * 43})
    run_admin("import", str(f))
    aws.keys_table().update_item(Key={"key_hash": by_owner["lu"]}, UpdateExpression="SET spent_micro = :s",
                                 ExpressionAttributeValues={":s": 1_250_000})
    out = run_admin("import", str(f), "--budget", "9")
    assert "imported 0 keys" in out and "1 keys of the file were already stored" in out
    item = _item(by_owner["lu"])
    assert item["spent_micro"] == 1_250_000 and item["budget_micro"] == 3_000_000


def test_import_revoke_missing_leaves_issued_keys(issue, tmp_path):
    issued_id, _ = issue(owner="mo")
    f, by_owner = _hashes_file(tmp_path, {"t12/ned": "e" * 43, "t12/oz": "f" * 43})
    run_admin("import", str(f))
    f.write_text(json.dumps({by_owner["t12/ned"]: "t12/ned"}))
    assert "; 0 revoked" in run_admin("import", str(f))  # without the flag a missing hash stays active
    assert _item(by_owner["t12/oz"])["state"] == "active"
    run_admin("import", str(f), "--revoke-missing")
    assert _item(by_owner["t12/oz"])["state"] == "revoked"
    assert _item(by_owner["t12/ned"])["state"] == "active"
    assert key_item(issued_id)["state"] == "active"


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"abc": "pat"}', json.dumps({"A" * 64: "pat"}),
                                     json.dumps({"0" * 64: ""})])
def test_import_refuses_a_bad_file(topic_arn, tmp_path, content):
    f = tmp_path / "bad.json"
    f.write_text(content)
    before = aws.keys_table().scan(Select="COUNT")["Count"]
    with pytest.raises(SystemExit):
        run_admin("import", str(f))
    assert aws.keys_table().scan(Select="COUNT")["Count"] == before


def test_import_refuses_two_hashes_with_one_key_id(topic_arn, tmp_path):
    f = tmp_path / "clash.json"
    f.write_text(json.dumps({"ab" * 5 + "0" * 54: "pia", "ab" * 5 + "1" * 54: "quin"}))
    with pytest.raises(SystemExit):
        run_admin("import", str(f))
    assert "Item" not in aws.keys_table().get_item(Key={"key_hash": "ab" * 5 + "0" * 54})
