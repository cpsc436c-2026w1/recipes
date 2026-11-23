"""The page's response-time check: one cold read of /api/zone-counts, then 100 reads, one a second, each after the answer.

    uv run python ext-dashboard-page/bench.py https://<url-id>.lambda-url.ca-central-1.on.aws/
    uv run python ext-dashboard-page/bench.py http://localhost:8080/ --reads 20      # the local page: no cold read

Run it from CloudShell in ca-central-1 (or a laptop: then the time includes your network), after the counts are
loaded, with the same PREFIX as the deploy. Clock: from the request leaving to the last byte of the answer. A non-200
answer is a failure and counts as over 1 s.

1. The cold read. The function `$PREFIX-page` (or --function) gets a new value of BENCH_COLD_START in its
   environment (UpdateFunctionConfiguration, the other variables kept); once LastUpdateStatus is Successful, the next
   call starts a new execution environment. That first read is reported on its own line and must be under 1,000 ms.
   The variable stays on the function until the next deploy. --no-cold skips this step (a local page has none).
2. The warm reads: the p95 is DuckDB's quantile_cont at 0.95 over them; under 1,000 ms passes.
The check passes when both do.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

import duckdb

LIMIT_MS = 1000.0
FAILED_MS = 1e9                # a failed read counts as over the limit
COLD_VAR = "BENCH_COLD_START"


def force_cold(lam, fn: str, sleep=time.sleep, now=lambda: datetime.now(timezone.utc), wait_s: float = 120) -> None:
    """Change COLD_VAR in the function's environment, keep the other variables, and wait until the update is done."""
    env = lam.get_function_configuration(FunctionName=fn).get("Environment", {}).get("Variables", {})
    env = env | {COLD_VAR: now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
    waited = 0.0
    while True:
        status = lam.get_function_configuration(FunctionName=fn).get("LastUpdateStatus")
        if status == "Successful":
            return
        if status == "Failed" or waited >= wait_s:
            raise RuntimeError(f"the update of {fn} ended {status} after {waited:g} s")
        sleep(1)
        waited += 1


def read_once(url: str, clock=time.perf_counter) -> tuple[float, int]:
    start = clock()
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            r.read()
            status = r.status
    except urllib.error.HTTPError as err:
        err.read()
        status = err.code
    except OSError:
        status = 0
    return (clock() - start) * 1000, status


def bench(base: str, reads: int = 100, every_s: float = 1.0, read=read_once, sleep=time.sleep, cold=None) -> dict:
    """With `cold` (a function that forces a cold start), one cold read first, judged on its own."""
    url = base.rstrip("/") + "/api/zone-counts"
    out = {}
    if cold is not None:
        cold()
        ms, status = read(url)
        ms = ms if status == 200 else FAILED_MS
        out = {"cold_ms": round(ms, 1), "cold_status": status, "cold_passed": ms < LIMIT_MS}
        sleep(every_s)
    rows = []
    for _ in range(reads):
        begun = time.monotonic()
        ms, status = read(url)
        rows.append((ms, status))
        sleep(max(0.0, every_s - (time.monotonic() - begun)))
    con = duckdb.connect()
    con.execute("CREATE TABLE reads (ms DOUBLE, status INTEGER)")
    con.executemany("INSERT INTO reads VALUES (?, ?)", rows)
    n, failed, p50, p95 = con.execute(f"""
        SELECT count(*), count(*) FILTER (WHERE status <> 200),
               quantile_cont(CASE WHEN status = 200 THEN ms ELSE {FAILED_MS} END, 0.5),
               quantile_cont(CASE WHEN status = 200 THEN ms ELSE {FAILED_MS} END, 0.95)
        FROM reads""").fetchone()
    return out | {"reads": n, "failed": failed, "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
                  "passed": p95 < LIMIT_MS and out.get("cold_passed", True)}


def is_local(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--reads", type=int, default=100)
    p.add_argument("--every", type=float, default=1.0, help="seconds between the starts of two reads")
    p.add_argument("--function", help="the page's function, default $PREFIX-page")
    p.add_argument("--no-cold", action="store_true", help="skip the cold read (always skipped for localhost)")
    a = p.parse_args(argv)
    cold = None
    if not (a.no_cold or is_local(a.url)):
        import boto3
        fn = a.function or f"{os.environ.get('PREFIX') or 'recipe'}-page"
        lam = boto3.session.Session(region_name=os.environ.get("AWS_REGION") or "ca-central-1").client("lambda")
        cold = lambda: force_cold(lam, fn)  # noqa: E731
        print(f"forcing a cold start of {fn} ({COLD_VAR} changed), then one read")
    try:
        out = bench(a.url, a.reads, a.every, cold=cold)
    except Exception as err:
        if cold is None:
            raise
        print(f"FAIL: the cold start was not forced ({type(err).__name__}: {err}); --no-cold checks the warm "
              "reads only")
        return 1
    if "cold_ms" in out:
        took = f"{out['cold_ms']} ms" if out["cold_status"] == 200 else "failed"
        print(f"cold read: {took} (HTTP {out['cold_status']}): "
              f"{'PASS' if out['cold_passed'] else 'FAIL'} (under {LIMIT_MS:,.0f} ms)")
    print(f"{out['reads']} reads, {out['failed']} failed; p50 {out['p50_ms']} ms, p95 {out['p95_ms']} ms: "
          f"{'PASS' if out['p95_ms'] < LIMIT_MS else 'FAIL'} (p95 under {LIMIT_MS:,.0f} ms)")
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
