"""lake.py: the shared bucket up and down by the ledger, and ensure_workgroup's three names (in moto)."""
import pytest

import lake
import tags

BUCKET = "recipe-test-lake-123456789012"       # moto's account


def test_up_creates_tagged_bucket_with_lifecycle(aws):
    sent = []
    # moto drops tags given in CreateBucketConfiguration, so the test reads what the call carried.
    aws.events.register("before-parameter-build.s3.CreateBucket", lambda params, **_: sent.append(params))
    assert lake.up(aws, log=lambda *_: None) == "created"
    s3 = aws.client("s3")
    config = sent[0]["CreateBucketConfiguration"]
    assert config["LocationConstraint"] == "ca-central-1"
    assert {t["Key"]: t["Value"] for t in config["Tags"]} == {"Project": "436c", "Team": "recipe-test",
                                                             "Owner": "tester"}
    rules = {r["ID"]: r for r in s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]}
    assert rules["athena-results-7-days"]["Expiration"]["Days"] == 7
    assert s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]["BlockPublicPolicy"]
    assert tags.recorded("s3-bucket", BUCKET) == "created"
    assert lake.up(aws, log=lambda *_: None) == "created"       # a second up changes nothing
    assert len(tags.ledger_rows()) == 1


def test_a_bucket_that_was_there_is_borrowed_and_kept(aws):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    s3.put_object(Bucket=BUCKET, Key="raw/trip-events/dt=2026-11-23/20261123T0900Z-r1.jsonl", Body=b"{}\n")
    s3.put_object(Bucket=BUCKET, Key="raw/trip-events/dt=2026-11-23/a.jsonl", Body=b"{}\n")
    s3.put_object(Bucket=BUCKET, Key="team/own.csv", Body=b"x")
    assert lake.up(aws, log=lambda *_: None) == "borrowed"
    tags.record("s3-replay", "r1", "created", script="test")
    assert lake.down(aws, yes=True, log=lambda *_: None) == 0
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"])
    assert keys == ["raw/trip-events/dt=2026-11-23/a.jsonl", "team/own.csv"]   # only this ledger's replay went


def test_own_raw_is_what_the_ledger_records(tmp_path):
    rows = [{"type": "s3-replay", "name": "r1", "state": "created"},
            {"type": "s3-replay", "name": "r2", "state": "created"}, {"type": "s3-replay", "name": "r2", "state": "deleted"},
            {"type": "s3-object", "name": "raw/trip-events/dt=2026-11-23/x-req1.jsonl", "state": "created"},
            {"type": "s3-prefix", "name": "raw/trip-events/dt=2026-11-22/20261122-", "state": "created"}]
    keys = ["raw/trip-events/dt=2026-11-23/20261123T0900Z-r1.jsonl",            # this ledger's replay
            "raw/trip-events/dt=2026-11-23/20261123T0930Z-r1-again.jsonl",      # a copy of one of its files
            "raw/trip-events/dt=2026-11-23/20261123T0900Z-r10.jsonl",           # another run whose id starts alike
            "raw/trip-events/dt=2026-11-23/20261123T0900Z-r2.jsonl",            # recorded, then deleted
            "raw/trip-events/dt=2026-11-23/x-req1.jsonl",                       # an object the ledger names
            "raw/trip-events/dt=2026-11-22/20261122-0001.parquet",              # under a recorded prefix
            "raw/trip-events/dt=2026-11-23/20261123T0900Z-teamrun.jsonl",       # a team's own pipeline's file
            "athena-results/q.csv"]
    assert lake.own_raw(keys, rows) == keys[:2] + keys[4:6]


def test_down_refuses_while_another_runs_raw_files_are_in_its_bucket(aws):
    lake.up(aws, log=lambda *_: None)
    s3 = aws.client("s3")
    s3.put_object(Bucket=BUCKET, Key="raw/trip-events/dt=2026-11-23/20261123T0900Z-other.jsonl", Body=b"{}\n")
    s3.put_object(Bucket=BUCKET, Key="raw/trip-events/dt=2026-11-23/20261123T0900Z-mine.jsonl", Body=b"{}\n")
    tags.record("s3-replay", "mine", "created", script="test")
    out = []
    assert lake.down(aws, yes=True, log=out.append) == 1
    assert "20261123T0900Z-other.jsonl" in "\n".join(out)
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"])
    assert len(keys) == 2                                        # nothing deleted while another run's file is left


def test_down_refuses_while_another_prefix_is_left(aws):
    lake.up(aws, log=lambda *_: None)
    s3 = aws.client("s3")
    s3.put_object(Bucket=BUCKET, Key="lake/merge-state/dt=2026-11-23.txt", Body=b"x")
    out = []
    assert lake.down(aws, yes=True, log=out.append) == 1
    assert "lake/merge-state/dt=2026-11-23.txt" in "\n".join(out)
    s3.head_bucket(Bucket=BUCKET)                                # still there


def test_down_dry_run_deletes_nothing_then_yes_deletes(aws):
    lake.up(aws, log=lambda *_: None)
    lake.ensure_workgroup("recipe-test-wg", sess=aws)
    s3 = aws.client("s3")
    s3.put_object(Bucket=BUCKET, Key="athena-results/q.csv", Body=b"x")
    out = []
    assert lake.down(aws, log=out.append) == 0
    assert any("would remove" in line for line in out)
    s3.head_bucket(Bucket=BUCKET)
    assert lake.down(aws, yes=True, log=lambda *_: None) == 0
    assert BUCKET not in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "recipe-test-wg" not in [w["Name"] for w in aws.client("athena").list_work_groups()["WorkGroups"]]
    assert tags.recorded("s3-bucket", BUCKET) == "deleted"
    assert tags.recorded("athena-workgroup", "recipe-test-wg") == "deleted"


@pytest.mark.parametrize("name, cutoff, enforce, results", [
    ("recipe-test-wg", 2 ** 30, True, "athena-results/"),
    ("recipe-test-ctas-wg", 8 * 2 ** 30, False, "athena-results/"),
    ("recipe-test-chat-wg", 104_857_600, True, "athena-results/chat-wg/")])
def test_ensure_workgroup_creates_with_its_defaults(aws, name, cutoff, enforce, results):
    assert lake.ensure_workgroup(name, sess=aws) == "created"
    config = aws.client("athena").get_work_group(WorkGroup=name)["WorkGroup"]["Configuration"]
    assert config["BytesScannedCutoffPerQuery"] == cutoff
    assert config["EnforceWorkGroupConfiguration"] is enforce
    assert config["ResultConfiguration"]["OutputLocation"] == f"s3://{BUCKET}/{results}"
    assert lake.ensure_workgroup(name, sess=aws) == "created"   # a second call: no new row
    assert [r["state"] for r in tags.ledger_rows()] == ["created"]


def test_callers_give_no_numbers_of_their_own(aws):
    with pytest.raises(ValueError, match="callers do not give their own"):
        lake.ensure_workgroup("recipe-test-chat-wg", 2 ** 30, enforce=True, sess=aws)
    assert lake.ensure_workgroup("recipe-test-chat-wg", 104_857_600, enforce=True, sess=aws) == "created"
    with pytest.raises(ValueError, match="knows"):
        lake.ensure_workgroup("recipe-test-other-wg", sess=aws)


def test_an_existing_workgroup_with_other_settings_stops_and_is_not_changed(aws):
    athena = aws.client("athena")
    athena.create_work_group(Name="recipe-test-wg", Configuration={
        "ResultConfiguration": {"OutputLocation": f"s3://{BUCKET}/athena-results/"},
        "EnforceWorkGroupConfiguration": False, "BytesScannedCutoffPerQuery": 10 * 2 ** 20,
        "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"}})
    with pytest.raises(lake.WorkgroupMismatch) as err:
        lake.ensure_workgroup("recipe-test-wg", sess=aws)
    assert "enforced is False, not True" in str(err.value) and "cutoff (bytes)" in str(err.value)
    assert athena.get_work_group(WorkGroup="recipe-test-wg")["WorkGroup"]["Configuration"][
        "BytesScannedCutoffPerQuery"] == 10 * 2 ** 20
    assert tags.ledger_rows() == []


def test_a_matching_workgroup_made_elsewhere_is_borrowed_and_kept_by_down(aws):
    lake.up(aws, log=lambda *_: None)
    athena = aws.client("athena")
    athena.create_work_group(Name="recipe-test-ctas-wg", Configuration={
        "ResultConfiguration": {"OutputLocation": f"s3://{BUCKET}/athena-results/"},
        "EnforceWorkGroupConfiguration": False, "BytesScannedCutoffPerQuery": 8 * 2 ** 30,
        "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"}})
    assert lake.ensure_workgroup("recipe-test-ctas-wg", sess=aws) == "borrowed"
    lake.down(aws, yes=True, log=lambda *_: None)
    assert "recipe-test-ctas-wg" in [w["Name"] for w in athena.list_work_groups()["WorkGroups"]]


def test_down_keeps_the_chat_workgroup_its_recipe_owns(aws):
    lake.up(aws, log=lambda *_: None)
    lake.ensure_workgroup("recipe-test-chat-wg", sess=aws)
    lake.down(aws, yes=True, log=lambda *_: None)
    assert "recipe-test-chat-wg" in [w["Name"] for w in aws.client("athena").list_work_groups()["WorkGroups"]]


def test_a_workgroup_another_recipe_created_is_deleted_by_down_only(aws):
    """A caller in another recipe (lake-layout, say) creates the shared workgroup through ensure_workgroup: the
    ledger row is lake.py's, its clean-up has no step for it, and down deletes it."""
    lake.up(aws, log=lambda *_: None)
    lake.ensure_workgroup("recipe-test-ctas-wg", sess=aws)
    row = tags.ledger_rows()[-1]
    assert row["script"] == "lake.py ensure_workgroup" and row["clean"] == "scripts/lake.py down"
    lake.down(aws, yes=True, log=lambda *_: None)
    assert "recipe-test-ctas-wg" not in [w["Name"] for w in aws.client("athena").list_work_groups()["WorkGroups"]]
