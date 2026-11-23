"""The read-path experiment: planned calls to the API's URL, one at a time, joined with their REPORT lines in DuckDB.

    bash ext-read-path/run.sh                                   # 200 calls of each variant in variants.toml
    bash ext-read-path/run.sh --busy-rider --writer 5
    bash ext-read-path/run.sh --idle                            # one after-idle call, when its gap has passed
    bash ext-read-path/run.sh --no-plan --fresh 20              # after the idle round (a configuration change)
    bash ext-read-path/run.sh --summary                         # the summary of results.duckdb, as Markdown

What it changes in AWS: with --busy-rider, the 300 trips of rider r-busy in <PREFIX>-rider-trips (written before
the calls, deleted after them, also when a call failed); with --fresh, the function's variable READ_PATH_ROUND
(removed at the end). Both are written to the ledger state/ledger.jsonl while they exist, so a stopped run is
undone by `uv run python clean_up.py --yes`. Locally it writes results.duckdb (RESULTS sets another path).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
sys.path[:0] = [str(HERE), str(RECIPE / "scripts")]
import report_lines  # noqa: E402
import writer as writer_mod  # noqa: E402
from tags import prefix, record, recorded  # noqa: E402

SEED = 436
CALLS_PER_VARIANT = 200
IDLE_GAPS = [5, 15, 30, 60]          # minutes; five rounds of the four gaps: 20 calls
IDLE_ROUNDS = 5
REGION = "ca-central-1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  run_id VARCHAR, seq INTEGER, kind VARCHAR, variant VARCHAR, action VARCHAR, key VARCHAR, read VARCHAR,
  writer VARCHAR, gap_min INTEGER, round INTEGER, sent_at TIMESTAMPTZ, status INTEGER, caller_ms DOUBLE,
  read_units DOUBLE, request_id VARCHAR, class VARCHAR, duration_ms DOUBLE, billed_ms DOUBLE, memory_mb INTEGER,
  init_ms DOUBLE);
CREATE TABLE IF NOT EXISTS writer_runs (
  run_id VARCHAR, updates_done INTEGER, throttled INTEGER, retried INTEGER, failed INTEGER, max_per_second INTEGER);
"""


# ---- the plan

@dataclass
class Call:
    variant: str
    action: str
    key: str
    body: dict
    url: str
    read: str = ""
    kind: str = "planned"
    writer: str = "off"
    gap_min: int | None = None
    round: int | None = None
    result: dict = field(default_factory=dict)


def key_list(spec) -> list[str]:
    """A variant's keys: a list, or "trips:50" (t-0000 ...) or "riders:5" (r-00 ...), the dynamodb recipe's items."""
    if isinstance(spec, list):
        return [str(k) for k in spec]
    kind, _, n = str(spec).partition(":")
    if kind == "trips":
        return [f"t-{i:04d}" for i in range(int(n or 50))]
    if kind == "riders":
        return [f"r-{i:02d}" for i in range(int(n or 5))]
    raise ValueError(f"keys must be a list, trips:<n> or riders:<n>, not {spec!r}")


def body_of(action: str, key: str, extra: dict) -> dict:
    field_name = {"get": "trip_id", "rider": "rider_id"}.get(action, extra.get("key_field", "key"))
    return {"action": action, field_name: key, **{k: v for k, v in extra.items() if k != "key_field"}}


def load_variants(path: Path, default_url: str) -> list[dict]:
    variants = tomllib.loads(path.read_text())["variant"]
    for v in variants:
        v["url"] = os.path.expandvars(v.get("url", "")) or default_url
    return variants


def plan(variants: list[dict], seed: int = SEED) -> list[Call]:
    """CALLS_PER_VARIANT calls of each variant: every key the same number of times, in one order shuffled with the
    seed, so a re-run sends the same calls."""
    calls = []
    for v in variants:
        keys = key_list(v["keys"])
        n = int(v.get("calls", CALLS_PER_VARIANT))
        if n % len(keys):
            raise ValueError(f"variant {v['name']}: {n} calls do not divide over {len(keys)} keys")
        extra = dict(v.get("body", {}))
        for key in keys * (n // len(keys)):
            calls.append(Call(v["name"], v["action"], key, body_of(v["action"], key, extra), v["url"],
                              read=v["action"]))
    random.Random(seed).shuffle(calls)
    return calls


def busy_plan(url: str, writer: str = "off") -> list[Call]:
    return [Call("rider", "rider", writer_mod.BUSY_RIDER, {"action": "rider", "rider_id": writer_mod.BUSY_RIDER},
                 url, read=f"rider {writer_mod.BUSY_RIDER}", writer=writer) for _ in range(CALLS_PER_VARIANT)]


# ---- one call

def curl_call(url: str, body: dict) -> dict:
    """One POST with curl, waited for: its time from send to last byte, status, request id and reply."""
    with tempfile.TemporaryDirectory() as tmp:
        out, head = Path(tmp) / "body", Path(tmp) / "headers"
        p = subprocess.run(["curl", "-s", "-o", str(out), "-D", str(head), "-w", "%{time_total} %{http_code}",
                            "-X", "POST", "-H", "content-type: application/json", "--max-time", "30",
                            "-d", json.dumps(body), url], capture_output=True, text=True)
        total, _, code = (p.stdout.strip() or "0 0").partition(" ")
        request_id = None
        if head.exists():
            for line in head.read_text(errors="replace").splitlines():
                name, _, value = line.partition(":")
                if name.strip().lower() == "x-amzn-requestid":
                    request_id = value.strip()
        try:
            reply = json.loads(out.read_text()) if out.exists() else None
        except ValueError:
            reply = None
    return {"caller_ms": float(total) * 1000, "status": int(code or 0), "request_id": request_id, "reply": reply}


# ---- the results file

class Results:
    def __init__(self, path: Path):
        self.path = path
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def add(self, run_id: str, seq: int, c: Call, sent_at: datetime) -> None:
        r = c.result
        reply = r.get("reply") if isinstance(r.get("reply"), dict) else {}
        units = reply.get("read_units", reply.get("data_scanned_bytes"))
        self.con.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', NULL, NULL, NULL, NULL)",
                         [run_id, seq, c.kind, c.variant, c.action, c.key, c.read, c.writer, c.gap_min, c.round,
                          sent_at, r.get("status"), r.get("caller_ms"), units, r.get("request_id")])

    def join_reports(self, reports: dict[str, dict], run_id: str) -> None:
        for rid, rep in reports.items():
            self.con.execute("UPDATE calls SET class = ?, duration_ms = ?, billed_ms = ?, memory_mb = ?, init_ms = ? "
                             "WHERE request_id = ? AND run_id = ?",
                             [report_lines.call_class(rep), rep["duration_ms"], rep["billed_ms"], rep["memory_mb"],
                              rep["init_ms"], rid, run_id])

    def request_ids(self, run_id: str) -> set[str]:
        return {r[0] for r in self.con.execute(
            "SELECT request_id FROM calls WHERE run_id = ? AND request_id IS NOT NULL", [run_id]).fetchall()}

    def close(self):
        self.con.close()


# ---- the parts of a run

@dataclass
class Deps:
    """What a run talks to; the tests pass stand-ins (a fake URL, moto, a fake clock)."""
    send: object = curl_call
    sess: object = None
    now: object = lambda: datetime.now(timezone.utc)
    sleep: object = time.sleep
    start_writer: object = None
    log: object = print
    writer_clock: object = time.monotonic      # the writer's own clock (the tests give it a fake one)
    writer_sleep: object = time.sleep
    writer_ddb: object = None                  # the writer's client; default: adaptive retries, 10 attempts

    def client(self, service):
        if self.sess is None:
            import boto3
            self.sess = boto3.session.Session(region_name=REGION)
        return self.sess.client(service)


def send_all(calls: list[Call], results: Results, run_id: str, deps: Deps, start_seq: int = 0) -> int:
    """Each call after the answer of the one before (README, Decisions: one call at a time)."""
    for i, c in enumerate(calls, start_seq):
        sent_at = deps.now()
        c.result = deps.send(c.url, c.body)
        results.add(run_id, i, c, sent_at)
    return start_seq + len(calls)


def batch_write(ddb, table: str, requests: list[dict], sleep) -> None:
    """25 requests a call; unprocessed ones are sent again, as the dynamodb recipe's lib.sh does."""
    for i in range(0, len(requests), 25):
        left, wait = {table: requests[i:i + 25]}, 0.1
        while left:
            left = ddb.batch_write_item(RequestItems=left).get("UnprocessedItems") or {}
            if left:
                sleep(wait)
                wait = min(wait * 2, 5)


def busy_items(op: str) -> list[dict]:
    out = []
    for t in writer_mod.busy_trips():
        key = {"rider_id": {"S": t["rider_id"]}, "requested_at": {"S": t["requested_at"]}}
        if op == "put":
            out.append({"PutRequest": {"Item": key | {"trip_id": {"S": t["trip_id"]},
                                                      "pickup_zone": {"S": t["pickup_zone"]}}}})
        else:
            out.append({"DeleteRequest": {"Key": key}})
    return out


def thread_writer(table: str, rate: float, deps: Deps):
    """Run writer.run in this process (the tests); the default starts writer.py as a second process."""
    import threading
    box = {}
    t = threading.Thread(target=lambda: box.update(writer_mod.run(
        deps.writer_ddb or writer_mod.client(), table, rate, clock=deps.writer_clock, sleep=deps.writer_sleep)))
    t.start()

    def result():
        t.join()
        out = dict(box)
        out["max_per_second"] = writer_mod.max_per_second(out.pop("sent_at"))
        return out
    return result


def process_writer(table: str, rate: float, deps: Deps):
    p = subprocess.Popen([sys.executable, str(HERE / "writer.py"), "--table", table, "--rate", str(rate)],
                         stdout=subprocess.PIPE, text=True)

    def result():
        out, _ = p.communicate()
        return json.loads(out.strip().splitlines()[-1])
    return result


def not_ours(busy: bool, fresh: bool, fn: str, table: str, deps: Deps) -> list[str]:
    """What a run would overwrite and then delete although it did not make it (R321): items of r-busy that are
    already in the table, or a READ_PATH_ROUND the function already has. Asked before the first call. What a stopped
    run of this recipe left (its last ledger line `created`) is its own: the new run takes it over and undoes it."""
    found = []
    if busy and recorded("dynamodb-items", f"{table}:{writer_mod.BUSY_RIDER}") != "created":
        n = deps.client("dynamodb").query(
            TableName=table, KeyConditionExpression="rider_id = :r", Select="COUNT", Limit=1,
            ExpressionAttributeValues={":r": {"S": writer_mod.BUSY_RIDER}})["Count"]
        if n:
            found.append(f"rider {writer_mod.BUSY_RIDER} already has items in {table}: --busy-rider would overwrite "
                         "and then delete them")
    if fresh and recorded("lambda-env", f"{fn}:READ_PATH_ROUND") != "created":
        env = deps.client("lambda").get_function_configuration(FunctionName=fn).get("Environment", {}) \
            .get("Variables", {})
        if "READ_PATH_ROUND" in env:
            found.append(f"{fn} already has the variable READ_PATH_ROUND: --fresh would change and then remove it")
    return found


def fresh_round(n: int, fn: str, url: str, results: Results, run_id: str, seq: int, deps: Deps) -> int:
    """Before each call, change READ_PATH_ROUND and wait until the update is done; remove it at the end."""
    lam = deps.client("lambda")
    var = "READ_PATH_ROUND"

    def set_env(value):
        env = lam.get_function_configuration(FunctionName=fn).get("Environment", {}).get("Variables", {})
        env = {k: v for k, v in env.items() if k != var} | ({var: value} if value is not None else {})
        lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
        while True:
            status = lam.get_function_configuration(FunctionName=fn).get("LastUpdateStatus")
            if status == "Successful":
                return
            if status == "Failed":
                raise RuntimeError(f"the update of {fn} failed")
            deps.sleep(1)

    try:
        for r in range(1, n + 1):
            try:
                set_env(str(r))
            except Exception as err:
                if r == 1 and "AccessDenied" in str(err):
                    deps.log(f"the fresh-copy round stops: UpdateFunctionConfiguration was refused ({err}); "
                             "the idle round's cold calls stand in for it")
                    return seq
                raise
            if r == 1:
                record("lambda-env", f"{fn}:{var}", "created", script="ext-read-path/run.sh --fresh",
                       clean="the same run, or clean_up.py")
            c = Call("get-eventual", "get", "t-0000", {"action": "get", "trip_id": "t-0000"}, url, read="get",
                     kind="fresh copy", round=r)
            seq = send_all([c], results, run_id, deps, seq)
    finally:
        try:
            set_env(None)
            record("lambda-env", f"{fn}:{var}", "deleted", script="ext-read-path/run.sh --fresh")
        except Exception as err:       # said, not hidden: clean_up.py removes it later
            deps.log(f"FAILED: {var} was not removed from {fn} ({err}); run: uv run python clean_up.py --yes")
    return seq


def idle(results: Results, url: str, run_id: str, deps: Deps) -> Call | None:
    """One after-idle call if the last call to the URL is at least the next gap old; else say when it is due."""
    done = results.con.execute("SELECT count(*) FROM calls WHERE kind = 'after idle'").fetchone()[0]
    if done >= len(IDLE_GAPS) * IDLE_ROUNDS:
        deps.log(f"the idle round is done: {done} after-idle calls")
        return None
    gap = IDLE_GAPS[done % len(IDLE_GAPS)]
    last = results.con.execute("SELECT max(sent_at) FROM calls").fetchone()[0]
    now = deps.now()
    if last is not None and now - last < timedelta(minutes=gap):
        due = last + timedelta(minutes=gap)
        deps.log(f"next call due at {due.astimezone(timezone.utc):%H:%M:%S} (UTC; gap {gap} min): start "
                 "run.sh --idle again then")
        return None
    key = f"t-{done % 50:04d}"
    c = Call("get-eventual", "get", key, {"action": "get", "trip_id": key}, url, read="get", kind="after idle",
             gap_min=gap)
    send_all([c], results, run_id, deps, done)
    nxt = IDLE_GAPS[(done + 1) % len(IDLE_GAPS)]
    deps.log(f"after-idle call {done + 1} of {len(IDLE_GAPS) * IDLE_ROUNDS} sent (gap {gap} min); "
             f"the next gap is {nxt} minutes")
    return c


# ---- the summary

def price_params(prices: dict) -> list[float]:
    """summary.sql's three rates from prices.toml (`part = [USD, per how many units]`): Lambda requests and read
    units per million, a Lambda GB-second on arm64."""
    r = prices["rates"]

    def per(name, units):
        usd, n = r[name]
        return usd / n * units
    return [per("Lambda requests", 1_000_000), per("Lambda GB-seconds, arm64", 1),
            per("DynamoDB read units", 1_000_000)]

def summary(con, prices: dict) -> str:
    """The table of summary.sql, and each after-idle and fresh-copy call on its own line, as Markdown."""
    rows = con.execute((HERE / "summary.sql").read_text(), price_params(prices)).fetchall()
    cols = [d[0] for d in con.description]

    def ms(v, n, need):
        return f"{v:.1f}" if v is not None else f"fewer than {need} calls"
    out = [f"Prices: {prices['sheet']} (../prices.toml); USD (CAD at {prices['cad_per_usd']}).", "",
           "| variant | read | class | writer | calls | failed | p50 ms | p95 ms | p99 ms | p50 Lambda ms | p95 Lambda ms | p99 Lambda ms | median read units | 1,000 calls USD (CAD) |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        r = dict(zip(cols, row))
        cost = "" if r["cost_1000_usd"] is None else f"{r['cost_1000_usd']:.6f} ({r['cost_1000_usd'] * prices['cad_per_usd']:.6f})"
        units = "" if r["median_read_units"] is None else f"{r['median_read_units']:.1f}"
        out.append(f"| {r['variant']} | {r['read']} | {r['class']} | {r['writer']} | {r['calls']} | {r['failed']} | "
                   f"{ms(r['p50_ms'], r['calls'], 1)} | {ms(r['p95_ms'], r['calls'], 20)} | {ms(r['p99_ms'], r['calls'], 100)} | "
                   f"{ms(r['p50_lambda_ms'], r['calls'], 1)} | {ms(r['p95_lambda_ms'], r['calls'], 20)} | "
                   f"{ms(r['p99_lambda_ms'], r['calls'], 100)} | {units} | {cost} |")
    slo = []
    for row in rows:
        r = dict(zip(cols, row))
        if r["class"] == "all" and r["calls"]:
            verdict = ("fewer than 100 calls" if r["p99_ms"] is None else
                       "met" if r["p99_ms"] < 1000 else "NOT met")
            slo.append(f"- {r['variant']}, {r['read']}, writer {r['writer']}: p99 under 1,000 ms over warm and cold "
                       f"calls: {verdict}")
    if slo:
        out += ["", "SLO (R220):", *slo]
    alone = con.execute("SELECT kind, gap_min, round, class, init_ms, caller_ms, status FROM calls "
                        "WHERE kind IN ('after idle', 'fresh copy') ORDER BY kind, sent_at").fetchall()
    if alone:
        out += ["", "| call | gap or round | class | init ms | caller ms | status |", "|---|---|---|---|---|---|"]
        for kind, gap, rnd, cls, init, caller, status in alone:
            out.append(f"| {kind} | {f'{gap} min' if kind == 'after idle' else f'round {rnd}'} | {cls} | "
                       f"{'' if init is None else f'{init:.1f}'} | {caller:.1f} | {status} |")
    w = con.execute("SELECT run_id, updates_done, throttled, retried, failed, max_per_second FROM writer_runs").fetchall()
    if w:
        out += ["", "| writer run | updates done | throttled | retried | failed | most in one second |", "|---|---|---|---|---|---|"]
        out += [f"| {a} | {b} | {c} | {d} | {e} | {f} |" for a, b, c, d, e, f in w]
    return "\n".join(out)


# ---- a whole run

def function_url(fn: str, deps: Deps) -> str:
    return deps.client("lambda").get_function_url_config(FunctionName=fn)["FunctionUrl"]


def main(argv=None, deps: Deps | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variants", type=Path, default=HERE / "variants.toml")
    p.add_argument("--busy-rider", action="store_true", help="300 trips of r-busy, and 200 rider calls for them")
    p.add_argument("--writer", type=float, metavar="RATE", help="with --busy-rider: the calls again while updates go on")
    p.add_argument("--fresh", type=int, metavar="N", help="N calls, each after a configuration change")
    p.add_argument("--idle", action="store_true", help="one after-idle call when its gap has passed")
    p.add_argument("--summary", action="store_true", help="only print the summary of results.duckdb")
    p.add_argument("--no-plan", action="store_true", help="skip the 200 calls of each variant")
    a = p.parse_args(argv)
    if a.writer and not a.busy_rider:
        p.error("--writer needs --busy-rider: it updates the busy rider's trips")
    deps = deps or Deps()
    path = Path(os.environ.get("RESULTS") or HERE / "results.duckdb")
    prices = tomllib.loads((RECIPE / "prices.toml").read_text())
    results = Results(path)
    try:
        if a.summary:
            deps.log(summary(results.con, prices))
            return 0
        fn, table = f"{prefix()}-api", f"{prefix()}-rider-trips"
        url = function_url(fn, deps)
        run_id = deps.now().strftime("%Y%m%dT%H%M%SZ")
        start_ms = int(deps.now().timestamp() * 1000) - 1000
        seq = 0
        if a.idle:
            idle(results, url, run_id, deps)
        elif found := not_ours(a.busy_rider, bool(a.fresh), fn, table, deps):
            for line in found:
                deps.log(f"STOPPED: {line}")
            deps.log("Nothing was sent or changed. If a stopped run of this recipe left them, "
                     "`uv run python clean_up.py` lists them; otherwise use another PREFIX.")
            return 1
        else:
            calls = [] if a.no_plan else plan(load_variants(a.variants, url))
            deps.log(f"run {run_id}: {len(calls)} planned calls, one at a time, to {fn}'s URL")
            seq = send_all(calls, results, run_id, deps)
            if a.busy_rider:
                ddb = deps.client("dynamodb")
                record("dynamodb-items", f"{table}:{writer_mod.BUSY_RIDER}", "created",
                       script="ext-read-path/run.sh --busy-rider", clean="the same run, or clean_up.py")
                try:
                    batch_write(ddb, table, busy_items("put"), deps.sleep)
                    deps.log(f"300 trips of {writer_mod.BUSY_RIDER} written to {table}")
                    seq = send_all(busy_plan(url), results, run_id, deps, seq)
                    if a.writer:
                        start = deps.start_writer or process_writer
                        result = start(table, a.writer, deps)
                        seq = send_all(busy_plan(url, "on"), results, run_id, deps, seq)
                        w = result()
                        results.con.execute("INSERT INTO writer_runs VALUES (?, ?, ?, ?, ?, ?)",
                                            [run_id, w["updates_done"], w["throttled"], w["retried"], w["failed"],
                                             w["max_per_second"]])
                        deps.log(f"writer: {w['updates_done']} updates, {w['throttled']} throttled, "
                                 f"{w['retried']} retried, at most {w['max_per_second']} in one second")
                finally:
                    batch_write(ddb, table, busy_items("delete"), deps.sleep)
                    record("dynamodb-items", f"{table}:{writer_mod.BUSY_RIDER}", "deleted",
                           script="ext-read-path/run.sh --busy-rider")
                    deps.log(f"the 300 trips of {writer_mod.BUSY_RIDER} deleted")
            if a.fresh:
                seq = fresh_round(a.fresh, fn, url, results, run_id, seq, deps)
        want = results.request_ids(run_id)
        if want:
            end_ms = int(deps.now().timestamp() * 1000) + 60_000
            reports = report_lines.fetch(deps.client("logs"), f"/aws/lambda/{fn}", start_ms, end_ms, want,
                                         sleep=deps.sleep)
            results.join_reports(reports, run_id)
            deps.log(f"{len(reports)} of {len(want)} calls joined with their REPORT line")
        deps.log(summary(results.con, prices))
        return 0
    finally:
        results.close()


if __name__ == "__main__":
    sys.exit(main())
