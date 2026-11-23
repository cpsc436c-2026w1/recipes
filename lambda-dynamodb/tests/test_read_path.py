"""ext-read-path offline: REPORT parsing, the curl call against a local server, the plan, and clean_up.py for a
stopped run."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import RECIPE

sys.path[:0] = [str(RECIPE / "ext-read-path"), str(RECIPE / "scripts")]

CAPTURED = ("REPORT RequestId: 3b93f44a-7fe9-40ee-b778-c5e1937b2692\tDuration: 316.02 ms\tBilled Duration: 810 ms\t"
            "Memory Size: 128 MB\tMax Memory Used: 92 MB\tInit Duration: 493.18 ms\t")


def test_report_line_of_the_captured_run():
    import report_lines
    r = report_lines.parse(CAPTURED)
    assert r == {"request_id": "3b93f44a-7fe9-40ee-b778-c5e1937b2692", "duration_ms": 316.02, "billed_ms": 810,
                 "memory_mb": 128, "max_memory_mb": 92, "init_ms": 493.18}
    assert report_lines.call_class(r) == "cold" and report_lines.gb_seconds(810, 128) == 0.10125
    assert report_lines.parse('{"request_id": "x", "action": "get"}') is None


def test_the_captured_output_parses():
    """The REPORT lines of the recipe's captured ext-metrics run: one has an Init Duration (the first call)."""
    import report_lines
    text = (RECIPE / "output" / "ext-metrics.txt").read_text()
    found = [r for r in (report_lines.parse("REPORT" + part) for part in text.split("REPORT")[1:]) if r]
    assert len(found) >= 7 and report_lines.call_class(found[0]) == "cold"
    assert {report_lines.call_class(r) for r in found[1:]} >= {"warm"}


def test_curl_call_reads_time_status_request_id_and_reply():
    import read_path

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            out = json.dumps({"echo": body, "read_units": 0.5}).encode()
            self.send_response(200)
            self.send_header("x-amzn-RequestId", "abc-123")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = read_path.curl_call(f"http://127.0.0.1:{server.server_address[1]}/", {"action": "get", "trip_id": "t-0001"})
    finally:
        server.shutdown()
    assert r["status"] == 200 and r["request_id"] == "abc-123" and r["caller_ms"] > 0
    assert r["reply"] == {"echo": {"action": "get", "trip_id": "t-0001"}, "read_units": 0.5}


def test_plan_needs_calls_that_divide_over_the_keys():
    import read_path
    with pytest.raises(ValueError):
        read_path.plan([{"name": "x", "action": "get", "keys": ["a", "b", "c"], "url": "u"}])


def test_writer_updates_are_one_per_busy_trip():
    import writer
    ups = writer.updates()
    assert len(ups) == 300 and len({u["requested_at"] for u in ups}) == 300
    assert {u["requested_at"] for u in ups} == {t["requested_at"] for t in writer.busy_trips()}
    assert writer.updates() == ups and all(1 <= u["rating"] <= 5 for u in ups)


def test_writer_pace_holds_after_a_slow_answer():
    import writer

    class Clock:
        t = 0.0

        def now(self):
            return self.t

        def sleep(self, s):
            self.t += s

    clock = Clock()

    class Slow:
        class meta:
            class events:
                @staticmethod
                def register_first(*a, **k):
                    pass

        n = 0

        def update_item(self, **kw):
            self.n += 1
            if self.n == 3:
                clock.t += 2.5            # one slow answer: the next sends must not bunch up
            return {"ResponseMetadata": {}}

    out = writer.run(Slow(), "t", 5, clock=clock.now, sleep=clock.sleep)
    assert out["updates_done"] == 300 and writer.max_per_second(out["sent_at"]) <= 5


def test_clean_up_undoes_a_stopped_read_path_run(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws
    monkeypatch.setenv("PREFIX", "recipe")
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    with mock_aws():
        import clean_up
        import read_path
        import read_path_fakes as F
        import tags
        ddb = boto3.client("dynamodb", region_name="ca-central-1")
        F.tables(ddb)
        read_path.batch_write(ddb, "recipe-rider-trips", read_path.busy_items("put"), lambda s: None)
        tags.record("dynamodb-items", "recipe-rider-trips:r-busy", "created")
        steps = clean_up.clean(boto3.session.Session(region_name="ca-central-1"), yes=True, log=lambda *_: None,
                               main_api=False)
        assert "300 items of r-busy in recipe-rider-trips" in steps.removed, steps.removed
        left = ddb.query(TableName="recipe-rider-trips", KeyConditionExpression="rider_id = :r",
                         ExpressionAttributeValues={":r": {"S": "r-busy"}})["Count"]
        assert left == 0 and tags.recorded("dynamodb-items", "recipe-rider-trips:r-busy") == "deleted"
        assert ddb.query(TableName="recipe-rider-trips", KeyConditionExpression="rider_id = :r",
                         ExpressionAttributeValues={":r": {"S": "r-03"}})["Count"] == 10


def test_run_sh_dry_calls_nothing(tmp_path):
    import os
    import subprocess
    fake = tmp_path / "bin"
    fake.mkdir()
    for tool in ("aws", "curl"):
        (fake / tool).write_text("#!/bin/sh\necho called >&2\nexit 99\n")
        (fake / tool).chmod(0o755)
    env = os.environ | {"DRY": "1", "PATH": f"{fake}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", str(RECIPE / "ext-read-path" / "run.sh"), "--busy-rider"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and "called" not in p.stderr and "$ aws " in p.stdout
