"""The eval matrix with groups, the rides case: offline on the stub, through a gateway stand-in, and its stops."""

import json
import os
import subprocess
import sys

import pytest
from conftest import ROOT, load_script

matrix = load_script("ext-eval-matrix/matrix.py")
import rides  # noqa: E402  ext-eval-matrix/, on the path once matrix.py is loaded
import score  # noqa: E402
import templates  # noqa: E402

AFTER = str(ROOT / "ext-eval-matrix" / "data" / "after-example.txt")


def test_every_rides_case_has_a_group_and_a_reference_or_none():
    trips = rides.Trips()
    cases = rides.load_cases(rides.TEST, trips)
    assert len(cases) == 20 and trips.rows == 837
    by_group = {}
    for c in cases:
        by_group.setdefault(c["group"], []).append(c)
        assert ("reference" in c) == c["answerable"]
    assert {g: (len(cs), sum(not c["answerable"] for c in cs)) for g, cs in by_group.items()} == \
        {"one-zone": (5, 1), "time-window": (5, 1), "zone-compare": (5, 1), "fare-sum": (5, 1)}


def test_the_hour_holds_the_counts_of_the_course_example():
    trips = rides.Trips()
    counts = {z: trips.run_sql(f"SELECT COUNT(*) FROM trips WHERE pickup_zone_id = {z}")[0] for z in (48, 161, 230)}
    assert counts == {48: 304.0, 161: 191.0, 230: 342.0}   # cp1-example's SIM01 hour: 304, 191 and 342


def test_value_metric_whole_numbers_exactly_and_cents():
    run = lambda v: (lambda sql: (v, None))  # noqa: E731
    case = {"id": "c", "answerable": True, "reference": 19074.0}
    assert score.score_value(case, "SELECT 1", run(19074.0)).outcome == "correct"
    assert score.score_value(case, "SELECT 1", run(19075.0)).outcome == "wrong"
    fare = {"id": "f", "answerable": True, "reference": 1234.56}
    assert score.score_value(fare, "SELECT 1", run(1234.5649)).outcome == "correct"
    assert score.score_value(fare, "SELECT 1", run(1234.58)).outcome == "wrong"
    refused = score.score_value(case, "```\nCANNOT_ANSWER\n```", run(None))
    assert refused.outcome == "wrong" and refused.refused and refused.kind == "refused an answerable case"
    none = {"id": "n", "answerable": False}
    assert score.score_value(none, "CANNOT_ANSWER.", run(None)).outcome == "correct refusal"
    assert score.score_value(none, "SELECT 1", run(4345.0)).outcome == "wrong"
    assert score.score_value(case, "", run(None)).kind == "no reply"


def test_templates_make_no_model_call_and_refuse_what_they_do_not_match():
    trips = rides.Trips()
    assert trips.run_sql(templates.answer("How many trips were requested in zone 161 between 08:00 and 09:00?")) == \
        (191.0, None)
    assert templates.answer("Which zone had the most trips?") == "CANNOT_ANSWER"
    assert templates.answer("How many more trips were requested in zone 161 than in zone 48?") == "CANNOT_ANSWER"


def test_rides_matrix_offline(tmp_path):
    out = matrix.main(["--case", "rides", "--prompt", f"after={AFTER}", "--out", str(tmp_path),
                       "--compare", "qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after"])
    report = (out / "report.md").read_text()
    rows = [json.loads(x) for x in (out / "cases.jsonl").read_text().splitlines()]
    assert len(rows) == 300 and {r["cell"] for r in rows} == {"qwen3.6-35b-a3b/seed", "qwen3.6-35b-a3b/after",
                                                               "qwen3.8-27b/seed", "qwen3.8-27b/after",
                                                               "no-model/templates"}
    assert all(r["cad"] == 0 for r in rows if r["cell"] == "no-model/templates")
    assert all(r["cad"] > 0 for r in rows if r["cell"] != "no-model/templates")
    for name in ("qwen3.6-35b-a3b/seed against qwen3.6-35b-a3b/after", "no-model/templates against qwen3.8-27b/after"):
        assert f"### {name}" in report
    assert "| all | 8 of 20 | 19 of 20 | 11 | 0 | +0.55 | 0.0010 |  |" in report


def test_rides_matrix_through_a_gateway_stand_in(tmp_path, monkeypatch):
    from features.support import TOKEN, FakeGateway
    gw = FakeGateway()
    try:
        monkeypatch.setenv("COURSE_GATEWAY_URL", gw.url)
        monkeypatch.setenv("KB_MCP_TOKEN", TOKEN)
        out = matrix.main(["--case", "rides", "--target", "gateway", "--runs", "1",
                           "--models", "qwen3.6-35b-a3b,no-model", "--out", str(tmp_path)])
    finally:
        gw.close()
    assert len(gw.requests) == 20                                    # 20 cases, 1 run, one model cell
    assert {(r["path"], r["auth"]) for r in gw.requests} == {("/v1/chat/completions", f"Bearer {TOKEN}")}
    assert {r["body"]["model"] for r in gw.requests} == {"qwen3.6-35b-a3b"}
    assert all("Table trips" in r["body"]["messages"][0]["content"] for r in gw.requests)
    assert "| qwen3.6-35b-a3b | seed |" in (out / "report.md").read_text()


def test_a_case_without_a_group_exits_2_before_any_call(tmp_path):
    bad = tmp_path / "test.yml"
    bad.write_text("- id: q01\n  group: one-zone\n  question: How many trips were requested in zone 161?\n"
                   "- id: q02\n  question: How many trips were requested before 08:00?\n")
    env = {k: v for k, v in os.environ.items() if k not in ("COURSE_GATEWAY_URL", "KB_MCP_TOKEN")}
    done = subprocess.run([sys.executable, "ext-eval-matrix/matrix.py", "--case", "rides", "--target", "gateway",
                           "--cases", str(bad), "--out", str(tmp_path / "out")], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    # exit 2 and the case named, before the gateway check that would ask for COURSE_GATEWAY_URL
    assert done.returncode == 2 and "case q02" in done.stderr and "Traceback" not in done.stderr, done.stderr
    assert not (tmp_path / "out").exists()


def test_compare_names_cells_of_the_matrix(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        matrix.main(["--case", "rides", "--compare", "qwen3.6-35b-a3b/seed,nope/x", "--out", str(tmp_path)])
    assert e.value.code == 2 and "name two cells" in capsys.readouterr().err


def test_no_model_cell_is_for_the_rides_case_only(tmp_path):
    with pytest.raises(SystemExit, match="rides case only"):
        matrix.main(["--models", "no-model", "--out", str(tmp_path)])


def test_the_day_builder_runs_on_a_sample_of_ds01_columns(tmp_path):
    """make_trips_day.sql on four made-up rows with DS01's column names (the real month is 500 MB over HTTPS), and
    the parquet it writes loads as the rides table."""
    import duckdb
    src, out = tmp_path / "month.parquet", tmp_path / "trips.parquet"
    duckdb.execute(f"""COPY (SELECT * FROM (VALUES
        ('HV0003', TIMESTAMP '2024-03-14 08:00:05', TIMESTAMP '2024-03-14 08:03:00', TIMESTAMP '2024-03-14 08:20:00',
         161, 230, 2.5, 1020, 15.25, 11.0),
        ('HV0003', TIMESTAMP '2024-03-14 23:59:59', TIMESTAMP '2024-03-15 00:04:00', TIMESTAMP '2024-03-15 00:30:00',
         48, 1, 10.0, 1560, 40.0, 30.0),
        ('HV0003', TIMESTAMP '2024-03-14 08:30:00', TIMESTAMP '2024-03-14 08:33:00', TIMESTAMP '2024-03-14 08:50:00',
         230, 161, 6.0, 1020, 21.5, 16.0),
        ('HV0005', TIMESTAMP '2024-03-14 09:00:00', TIMESTAMP '2024-03-14 09:05:00', TIMESTAMP '2024-03-14 09:15:00',
         230, 48, 1.0, 600, 9.0, 7.0))
        AS t(hvfhs_license_num, request_datetime, pickup_datetime, dropoff_datetime, PULocationID, DOLocationID,
             trip_miles, trip_time, base_passenger_fare, driver_pay)) TO '{src}' (FORMAT parquet)""")
    sql = (ROOT / "ext-eval-matrix" / "data" / "make_trips_day.sql").read_text()
    url = "https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet"
    assert url in sql and "'ext-eval-matrix/data/trips.parquet'" in sql
    duckdb.execute(sql.replace(url, str(src)).replace("'ext-eval-matrix/data/trips.parquet'", f"'{out}'"))
    trips = rides.Trips(out)
    assert trips.rows == 3                                         # Lyft's row (HV0005) left out
    cols = trips.con.execute("SELECT * FROM trips LIMIT 0").description
    assert [c[0] for c in cols] == ["trip_id", "trip_state", "pickup_zone_id", "requested_time", "driver_id", "fare",
                                    "trip_miles"]
    cases = rides.load_cases(rides.TEST, trips)                     # every reference query runs on the day's columns
    assert next(c for c in cases if c["id"] == "o1")["reference"] == 1.0
