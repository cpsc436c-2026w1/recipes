"""The real scripts and the real AWS CLI against a local stand-in for DynamoDB (moto server).

These check outcomes, not units: moto does not bill, so its read and write units differ from AWS's.
The Athena half of ext-nightly-report has no local stand-in; its commands are checked in test_dry_run.py.
Each AWS CLI call takes about a second to start, so this file takes a minute or so.
"""

import json
import subprocess
from pathlib import Path

from conftest import _base_env


def aws(env_url, *args):
    """One AWS CLI call against the moto server; returns the parsed JSON reply."""
    env = _base_env(Path("/tmp")) | {"AWS_ENDPOINT_URL": env_url, "AWS_PAGER": ""}
    p = subprocess.run(["aws", "dynamodb", *args, "--output", "json"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def get(url, table, key):
    return aws(url, "get-item", "--table-name", table, "--key", json.dumps(key), "--consistent-read")["Item"]


def test_race_one_driver_wins(local, moto_url):
    out = local("main/run.sh", KEEP="1")
    # Decision (conditional write): d-9's write is refused and its error carries the item that blocked it.
    assert "ConditionalCheckFailedException" in out.stderr and '"d-7"' in out.stderr
    item = get(moto_url, "recipe-trips", {"trip_id": {"S": "t-0042"}})
    assert item["driver_id"]["S"] == "d-7" and item["status"]["S"] == "accepted"
    # Decision (keys): the question that names no key reads every item.
    assert '"read": 50' in out.stdout and '"returned": 10' in out.stdout


def test_access_patterns_all_or_nothing_and_clean_up(local, moto_url):
    local("main/run.sh", KEEP="1")
    out = local("ext-riders-and-drivers/run.sh", KEEP="1")
    # Access pattern: one rider's trips for Oct 3 to Oct 6, read by key and sort-key range, in time order.
    hist = aws(moto_url, "query", "--table-name", "recipe-rider-trips",
               "--key-condition-expression", "rider_id = :r AND requested_at BETWEEN :a AND :b",
               "--expression-attribute-values",
               '{":r":{"S":"r-03"},":a":{"S":"2026-10-03"},":b":{"S":"2026-10-06T23:59:59Z"}}')
    times = [i["requested_at"]["S"] for i in hist["Items"]]
    assert len(times) == 4 and times == sorted(times) and hist["ScannedCount"] == 4
    # Decision (transaction): d-3 is busy, so neither the trip nor the driver changed.
    assert "TransactionCanceledException" in out.stderr
    t101 = get(moto_url, "recipe-trips", {"trip_id": {"S": "t-0101"}})
    assert "driver_id" not in t101 and t101["status"]["S"] == "requested"
    d3 = get(moto_url, "recipe-drivers", {"driver_id": {"S": "d-3"}})
    assert d3["trip_id"]["S"] == "t-0100" and d3["status"]["S"] == "busy"
    # Decision (optimistic locking): the first fare lands, the second (old version) is refused.
    t100 = get(moto_url, "recipe-trips", {"trip_id": {"S": "t-0100"}})
    assert t100["fare_quote"]["N"] == "24" and t100["version"]["N"] == "2"
    # Clean-up: every table the recipe made is gone.
    local("clean_up.sh")
    assert aws(moto_url, "list-tables")["TableNames"] == []
