"""Two clients race on one item: offline, against moto (one call at a time, as DynamoDB writes one item)."""

import boto3
import pytest
from moto import mock_aws

import local
import race
import trips

REGION = "ca-central-1"
TABLE = "recipe-test-race-trips"


@pytest.fixture
def make():
    with mock_aws():
        make = local.client_factory(REGION)
        trips.create_table(make(), TABLE)
        yield make


def test_unsafe_write_tells_both_drivers_yes(make):
    r = race.race_once(make, TABLE, "t-1", "unsafe")
    assert r["told_accepted"] == ["d-7", "d-9"]                 # a lost update: one of them is wrong
    assert r["stored_driver"] in ("d-7", "d-9")


def test_conditional_write_accepts_exactly_one(make):
    r = race.race_once(make, TABLE, "t-1", "safe")
    assert len(r["told_accepted"]) == 1
    winner = r["told_accepted"][0]
    loser = next(d for d in ("d-7", "d-9") if d != winner)
    assert r["stored_driver"] == winner and r["version"] == 2
    assert r["results"][loser] == {"driver": loser, "accepted": False, "held_by": winner}


def test_many_rounds(make):
    unsafe = race.race_rounds(make, TABLE, "unsafe", 10)
    safe = race.race_rounds(make, TABLE, "safe", 10)
    assert unsafe["both_told_yes"] == 10
    assert safe == {"rounds": 10, "one_winner": 10, "both_told_yes": 0, "winner_is_stored": 10}


def test_late_reader_sees_accepted_and_does_not_write(make):
    ddb = make()
    trips.reset_trip(ddb, TABLE, "t-2")
    assert trips.accept_safe(ddb, TABLE, "t-2", "d-7", 1)["accepted"]
    seen = trips.read_trip(ddb, TABLE, "t-2")
    assert seen == {"status": "accepted", "version": 2, "driver_id": "d-7", "tip_total": 0}


@mock_aws
def test_clean_up_removes_both_tables():
    import clean_up
    import tips
    s = boto3.Session(region_name=REGION)
    ddb = s.client("dynamodb")
    trips.create_table(ddb, trips.table_name("recipe-test"))
    tips.create_requests_table(ddb, tips.requests_table_name("recipe-test"))
    trips.create_table(ddb, trips.table_name("recipe-other"))
    clean_up.clean(s, "recipe-test")
    clean_up.clean(s, "recipe-test")
    assert ddb.list_tables()["TableNames"] == ["recipe-other-race-trips"]


def test_main_run_local(capsys):
    # by path: main/run.py and ext-idempotent-retry/run.py are both "run", and which one `import run` finds
    # depends on which test ran before this one
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("main_run", Path(__file__).parent.parent / "main" / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    out = run.main(["--target", "local", "--rounds", "5"])
    assert out["unsafe"]["both_told_yes"] == 5 and out["safe"]["one_winner"] == 5
    assert "refused, the trip is held by" in capsys.readouterr().out
