"""ext-nearby offline: the geohash code, the percentile, the data sets (made again, the same), and the dry runs.
The DynamoDB behaviour is in features/nearby.feature (moto inside the process)."""

import importlib.util
import json
import math
import os
import shutil
import sys

import pytest

from conftest import RECIPE

EXT = RECIPE / "ext-nearby"
sys.path.insert(0, str(EXT))
import nearby  # noqa: E402


def test_geohash_of_known_points():
    assert nearby.encode(40.7585, -73.9850, 6) == "dr5ru7"
    assert nearby.encode(40.7640, -73.9790, 6) == "dr5rus"
    assert nearby.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"      # the example of the geohash definition


def test_decode_is_the_cell_of_its_centre():
    for cell in ("dr5ru7", "dr5r", "u4pruydqqvj"):
        lat, lon, _, _ = nearby.decode(cell)
        assert nearby.encode(lat, lon, len(cell)) == cell


def test_rings_have_8k_cells_and_no_repeats():
    for k in (1, 2, 3):
        cells = nearby.ring("dr5ru7", k)
        assert len(cells) == len(set(cells)) == 8 * k
    assert "dr5ru7" not in nearby.ring("dr5ru7", 1)


def test_cell_sides_at_manhattan():
    sides = {p: round(nearby.shorter_side_m(nearby.encode(40.7580, -73.9855, p))) for p in (4, 5, 6, 7)}
    # the width is taken at the edge of the ring-1 block farthest from the equator, so precision 5 gives 3,698 m,
    # not the 3,701 m of the cell's own centre: the covered distance is never overstated
    assert sides == {4: 19546, 5: 3698, 6: 611, 7: 116}


def test_quantile_cont_is_linear_between_ranks():
    assert nearby.quantile_cont([1, 2, 3, 4], 0.5) == 2.5
    assert nearby.quantile_cont([100] * 828 + [1200] * 9, 0.99) == pytest.approx(804)
    assert nearby.quantile_cont([100] * 10 + [math.inf], 0.99) == math.inf


def test_the_data_sets_are_made_again_the_same(tmp_path):
    spec = importlib.util.spec_from_file_location("generate", EXT / "sim" / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    gen.main(tmp_path)
    for f in ("SIM01/requests.csv", "SIM02/drivers.csv", "SIM02/positions.csv", "SIM02/_truth/events.jsonl",
              "SIM02/_truth/faults.jsonl", "SIM03/points.csv", "SIM04/points.csv"):
        assert (tmp_path / f).read_text() == (EXT / "sim" / "data" / f).read_text(), f
    assert not (tmp_path / "SIM02" / "truth.json").exists() and not (tmp_path / "SIM02" / "faults.json").exists()
    assert nearby.sim_answer(tmp_path) == {"drivers": 60, "positions": 7200, "late": 72, "older_kept": 69,
                                           "cell_changes": 96, "cell_change_share": 0.0133}
    faults = [json.loads(x) for x in (tmp_path / "SIM02" / "_truth" / "faults.jsonl").read_text().splitlines()]
    assert len(faults) == 72 and {f["fault"] for f in faults} == {"late"}


def test_sim01_has_the_zones_of_the_rush_hour():
    reqs = nearby.sim_requests()
    zones = {}
    for r in reqs:
        zones[r["pickup_zone_id"]] = zones.get(r["pickup_zone_id"], 0) + 1
    assert len(reqs) == 837 and zones == {"48": 304, "161": 191, "230": 342}


def test_run_sh_runs_dry(dry):
    out = dry("ext-nearby/run.sh", KEEP="1").stdout
    for cmd in ("nearby.py tables", "nearby.py expect", "nearby.py seed", "nearby.py replay --speed 5",
                "nearby.py load", "nearby.py bench"):
        assert cmd in out, cmd
    assert "seeded" not in out                      # printed, not run


def test_clean_up_sh_runs_the_extension_clean_up_when_it_has_a_ledger(dry, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    assert "ext-nearby/clean_up.py" not in dry("clean_up.sh", LEDGER=str(ledger)).stdout
    ledger.write_text('{"type": "dynamodb-table", "name": "recipe-drivers-by-cell", "state": "created"}\n')
    assert "ext-nearby/clean_up.py --yes" in dry("clean_up.sh", LEDGER=str(ledger)).stdout


def test_the_shared_helper_copy_is_there():
    # the copy must stay byte-for-byte the shared one; tools/check_shared.py compares it once it is listed
    assert (EXT / "scripts" / "tags.py").read_text().startswith('"""The course tags for every create call')


class _Cancelling:
    """A client whose TransactWriteItems is cancelled with the given reasons for the first calls, then passed to the
    real (moto) client."""

    def __init__(self, real, reasons, times):
        self.real, self.reasons, self.times, self.calls = real, reasons, times, 0
        self.exceptions = real.exceptions

    def __getattr__(self, name):
        return getattr(self.real, name)

    def transact_write_items(self, **kw):
        self.calls += 1
        if self.calls <= self.times:
            from botocore.exceptions import ClientError

            err = {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                   "CancellationReasons": self.reasons}
            raise self.real.exceptions.TransactionCanceledException(err, "TransactWriteItems")
        return self.real.transact_write_items(**kw)


@pytest.fixture
def ddb(tmp_path, monkeypatch):
    from moto import mock_aws

    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_REGION": "ca-central-1",
                 "LEDGER": str(tmp_path / "ledger.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PREFIX", raising=False)
    with mock_aws():
        c = nearby.client()
        c.create_table(TableName="recipe-drivers", BillingMode="PAY_PER_REQUEST",
                       KeySchema=[{"AttributeName": "driver_id", "KeyType": "HASH"}],
                       AttributeDefinitions=[{"AttributeName": "driver_id", "AttributeType": "S"}])
        nearby.tables(c)
        yield c


def test_a_transaction_conflict_is_retried_by_the_code(ddb):
    c = _Cancelling(ddb, [{"Code": "TransactionConflict"}, {"Code": "None"}], times=2)
    r = nearby.put_position(c, "d-1", "standard", 40.7585, -73.9850, "2026-11-23T09:10:00Z", sleep=lambda s: None)
    assert r["result"] == "stored" and c.calls == 3
    assert [a["committed"] for a in r["attempts"]] == [False, False, True]


def test_a_conflict_that_does_not_end_is_raised(ddb):
    c = _Cancelling(ddb, [{"Code": "TransactionConflict"}, {"Code": "None"}], times=99)
    with pytest.raises(ddb.exceptions.TransactionCanceledException):
        nearby.put_position(c, "d-1", "standard", 40.7585, -73.9850, "2026-11-23T09:10:00Z", sleep=lambda s: None)
    assert c.calls == nearby.CONFLICT_ATTEMPTS


def test_at_most_3_retries_from_the_stored_driver(ddb):
    older = {"driver_id": {"S": "d-1"}, "cell": {"S": "dr5rus"}, "car_type": {"S": "standard"},
             "lat": {"N": "40.764"}, "lon": {"N": "-73.979"}, "position_time": {"S": "2026-11-23T09:09:00.000Z"}}
    c = _Cancelling(ddb, [{"Code": "ConditionalCheckFailed", "Item": older}, {"Code": "None"}], times=99)
    with pytest.raises(RuntimeError, match="gave up after 3 retries"):
        nearby.put_position(c, "d-1", "standard", 40.7585, -73.9850, "2026-11-23T09:10:00Z")
    assert c.calls == 1 + nearby.TX_RETRIES


class _OldSDK:
    """A client as boto3 1.34.46 builds it: CreateTable does not know OnDemandThroughput. Every call to
    AWS is recorded, so the test sees that none is made."""

    def __init__(self, real):
        self.real, self.calls = real, []
        self.exceptions = real.exceptions
        members = {k: v for k, v in real.meta.service_model.operation_model("CreateTable").input_shape.members.items()
                   if k != "OnDemandThroughput"}
        shape = type("Shape", (), {"members": members})()
        op = type("Op", (), {"input_shape": shape})()
        model = type("Model", (), {"operation_model": lambda self, name: op})()
        self.meta = type("Meta", (), {"service_model": model})()

    def __getattr__(self, name):
        self.calls.append(name)
        return getattr(self.real, name)


def test_the_installed_sdk_knows_the_ceiling(ddb):
    assert nearby.sdk_has_ceiling(ddb)


def test_an_sdk_without_the_ceiling_stops_before_any_call_or_ledger_line(ddb, tmp_path, monkeypatch, capsys):
    # live run 2026-11-23: python3 with boto3 1.34.46 wrote a `borrowed` ledger line, then stopped in a traceback
    monkeypatch.setenv("PREFIX", "recipe-old")
    ledger = tmp_path / "old-ledger.jsonl"
    monkeypatch.setenv("LEDGER", str(ledger))
    old = _OldSDK(ddb)
    with pytest.raises(nearby.OldSDK, match="does not know OnDemandThroughput"):
        nearby.tables(old)
    with pytest.raises(nearby.OldSDK):
        nearby.ensure_table(old, "recipe-old-points-by-cell", "point_id")
    assert old.calls == [] and not ledger.exists()
    assert not [t for t in ddb.list_tables()["TableNames"] if t.startswith("recipe-old")]
    monkeypatch.setattr(nearby, "client", lambda: old)
    assert nearby.main(["tables"]) == 1
    assert "Nothing was created" in capsys.readouterr().err


def test_the_bench_reports_p95():
    b = nearby.bench_summary([100] * 795 + [1200] * 42)
    assert (b["p50_ms"], b["p95_ms"], b["p99_ms"], b["pass"]) == (100.0, 320.0, 1200.0, False)
    assert nearby.bench_summary([100] * 827, failures=10)["p95_ms"] == 100.0


def test_the_bench_prints_its_units_before_the_verdict(ddb, tmp_path, monkeypatch, capsys):
    # live run 2026-11-23: `nearby.py bench` returned before printing {"units": ...}, so the capture had no read units
    import functools

    (tmp_path / "SIM01").mkdir()
    rows = (nearby.DATA / "SIM01" / "requests.csv").read_text().splitlines()[:6]
    (tmp_path / "SIM01" / "requests.csv").write_text("\n".join(rows) + "\n")
    monkeypatch.setattr(nearby, "bench", functools.partial(nearby.bench, data=tmp_path, rate=0))
    monkeypatch.setattr(nearby, "client", lambda: ddb)
    monkeypatch.setattr(nearby, "UNITS", nearby.Units())
    assert nearby.main(["bench"]) == 0
    out = capsys.readouterr().out
    units = [json.loads(line) for line in out.splitlines() if line.startswith('{"units"')]
    assert len(units) == 1 and "query" in units[0]["units"]["read_by_kind"]
    assert out.rstrip().splitlines()[-1].startswith("PASS: p50 ") and " p95 " in out
