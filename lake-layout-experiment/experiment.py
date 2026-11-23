"""The lake layout experiment: four layouts, three sizes, three queries, two engines; every run in results.duckdb.

  uv run python experiment.py build [--max-usd 2.00] [--copies same|dated]   # DS01, growth, final tables, function
  uv run python experiment.py run                                            # 360 Athena runs, 90 DuckDB calls
  uv run python experiment.py summary                                        # summary.sql -> summary.md
  uv run python experiment.py late                                           # one late row in the 4 tables of 1 copy
  uv run python experiment.py clean [--yes]                                  # without --yes: list what it would drop

Add --offline to any of them to run on the local stand-in (local Parquet folders, DuckDB in place of Athena and the
function; the scenarios' SIM01 as DS01). Live, set PREFIX, AWS credentials for ca-central-1, MAX_USD (the build
picks its plan with it) and, when the Athena data source `course` is missing, COURSE_CATALOG_ID (the course
catalog's account id, from tutorial B3T1's setup_athena.sh). Read the probe and the bound in results.duckdb (table
growth) after the build; a different MAX_USD needs a new build (README, "Run it live").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path += [p for p in (str(HERE / "scripts"),) if p not in sys.path]

from layout import (BY_DAY, LAYOUTS, MB, TB, Growth, TableBytes, build_order, grow, plan_upper_bound_usd,  # noqa: E402
                    copies_word, run_plan, scaling_problems, table)
from store import connect, load_prices, store_athena, store_duckdb, store_rows, summary  # noqa: E402

STATE = HERE / "state" / "experiment.json"  # the live build's sizes; an offline build keeps its own (state_path)
RESULTS = HERE / "results.duckdb"
LOCAL_ROOT = HERE / "build" / "local"
DS01_ROWS = 6_045_943
# SIM02 "late row": zone 161, picked up 2024-03-14 at 08:15 New York time (EDT, UTC-4), as if it arrived late.
SIM02 = ("SELECT TIMESTAMP '2024-03-14 12:15:00' AS pickup_time, 161 AS pickup_zone_id, 230 AS dropoff_zone_id, "
         "1.8 AS trip_miles, 640 AS trip_time_s, 14.2 AS fare, 11.05 AS driver_pay, 1 AS copy_no, "
         "DATE '2024-03-14' AS dt")


def prices() -> dict:
    return tomllib.loads((HERE / "prices.toml").read_text())


def max_usd(arg: float | None) -> float:
    return arg if arg is not None else float(os.environ.get("MAX_USD", "2.00"))


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --- build (R10, R20, R30, R40) -------------------------------------------------------------------------------------

def state_path(offline: bool) -> Path:
    """Where build writes its sizes: an offline build next to its results, so it never replaces the live build's."""
    return LOCAL_ROOT / "experiment.json" if offline else STATE


def build(backend, limit_usd: float, *, dated: bool = False, source: Path | None = None, con=None,
          expect_rows: int | None = None, max_copies: int = 256, log=print, state: Path | None = None) -> Growth:
    rows = backend.extract_ds01(source)
    log(f"ds01: {rows:,} rows")
    if expect_rows is not None and rows != expect_rows:
        raise SystemExit(f"ds01 has {rows:,} rows, expected {expect_rows:,} (README, Data)")
    built: set[tuple[str, int]] = set()
    per_copy: dict[str, TableBytes] = {}

    def ensure(layout: str, k: int) -> None:
        if (layout, k) not in built:
            kinds = backend.build_table(layout, k, dated)
            built.add((layout, k))
            log(f"built {table(layout, k)} ({' + '.join(kinds)})")

    def probe(k: int) -> float:
        for layout in BY_DAY:
            ensure(layout, k)
        # bytes of one copy, from the largest by-day tables built so far; a flat table holds the same files' rows
        b = backend.table_bytes("hive-by-day", k)
        for layout in LAYOUTS:
            per_copy[layout] = b if layout != "iceberg-by-day" else backend.table_bytes("iceberg-by-day", k)
        mb = backend.probe_mb(k)
        log(f"probe at {k}: {mb:.2f} MB")
        return mb

    def bound(sizes: list[int]) -> float:
        usd = plan_upper_bound_usd(sizes, per_copy, prices()["athena_per_tb"])
        log(f"upper bound of the plan {sizes}: {usd:.2f} USD")
        return usd

    g = grow(probe, bound, limit_usd, max_copies)
    log(f"growth: {g.note}; sizes {g.sizes or 'none'}")
    scanned: dict[tuple[str, int], float] = {}
    for k in g.sizes:
        for layout in build_order():
            ensure(layout, k)
            if expect_rows is not None or source is not None:
                n = backend.count(layout, k)
                if n != k * rows:
                    raise SystemExit(f"{table(layout, k)} has {n:,} rows, expected {k} x {rows:,}")
            scanned[(layout, k)] = backend.run_query(layout, k, "whole-table")["data_scanned_bytes"] / MB
        log(f"whole-table scan at {copies_word(k)}: " + ", ".join(f"{lay} {scanned[(lay, k)]:.2f} MB"
                                                                  for lay in LAYOUTS))
        wrong = scaling_problems(scanned, g.sizes)
        if wrong:  # a table that does not grow with its copies measures nothing the experiment asks (the live build)
            raise SystemExit("copies that take no room: " + "; ".join(wrong))
    if con is not None:
        rid = run_id()
        for k, mb in g.probes.items():
            con.execute("INSERT INTO growth VALUES (?, ?, ?, ?, ?, ?)",
                        [rid, k, mb, g.note, json.dumps(g.sizes), g.final_bound_usd])
    state = state or STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"sizes": g.sizes, "note": g.note, "probes": g.probes,
                                 "bound_usd": g.final_bound_usd, "dated": dated}))
    return g


# --- run (R50, R60, R70, R100) --------------------------------------------------------------------------------------

def run(backend, sizes: list[int], con, *, duckdb_engine: bool = True, log=print) -> int:
    plan = run_plan(sizes, duckdb_engine=duckdb_engine)
    rid = run_id()
    cold = True
    for i, r in enumerate(plan, 1):
        try:
            out = (backend.run_query(r.layout, r.copies, r.query) if r.engine == "athena"
                   else backend.duckdb_call(r.copies, r.query, cold))
        except Exception as e:  # one failed call is one excluded run, not the end of the run (R70)
            msg = f"{type(e).__name__}: {e}"[:200]
            out = {"query_execution_id": "", "state": "FAILED", "reason": msg, "reused": False,
                   "data_scanned_bytes": 0, "engine_ms": None, "queue_ms": None, "planning_ms": None,
                   "to_last_row_ms": None, "rows": [], "request_id": "", "duration_ms": None,
                   "billed_duration_ms": None, "init_duration_ms": None, "memory_mb": 2048, "s3_gets": None,
                   "error": msg}
        if r.engine == "athena":
            store_athena(con, rid, r, query_execution_id=out["query_execution_id"], state=out["state"],
                         reason=out["reason"], reused=out["reused"], data_scanned_bytes=out["data_scanned_bytes"],
                         engine_ms=out["engine_ms"], queue_ms=out["queue_ms"], planning_ms=out["planning_ms"],
                         to_last_row_ms=out["to_last_row_ms"])
        else:
            cold = False
            store_duckdb(con, rid, r, request_id=out["request_id"], duration_ms=out["duration_ms"],
                         billed_duration_ms=out["billed_duration_ms"], init_duration_ms=out["init_duration_ms"],
                         memory_mb=out["memory_mb"], s3_gets=out["s3_gets"], result_rows=len(out["rows"]),
                         to_last_row_ms=out["to_last_row_ms"], error=out.get("error", ""),
                         s3_lists=out.get("s3_lists"))
        store_rows(con, rid, r, r.engine, out["rows"])
        if i % 50 == 0:
            log(f"{i}/{len(plan)} runs")
    log(f"{len(plan)} runs stored in {RESULTS.name}")
    return len(plan)


# --- summary (R80, R90, R110, R120) ---------------------------------------------------------------------------------

def write_summary(con, out: Path, note: str = "") -> str:
    cols, rows = summary(con)
    lines = [f"# Summary of the lake layout experiment", "",
             f"Every number below is a value of `summary.sql` over `results.duckdb`. {note}".rstrip(), "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    text = "\n".join(lines) + "\n"
    out.write_text(text)
    return text


# --- late row (R130) ------------------------------------------------------------------------------------------------

def late(backend, zone: int = 161) -> dict:
    before = {lay: backend.one_day_count(lay, 1, zone) for lay in LAYOUTS}
    backend.late_insert(SIM02, 1)
    after = {lay: backend.one_day_count(lay, 1, zone) for lay in LAYOUTS}
    return {"before": before, "after": after, "agree": all(after[x] == before[x] + 1 for x in LAYOUTS)}


# --- live set-up ----------------------------------------------------------------------------------------------------

def live_backend():
    import boto3
    import lake

    from backends import AthenaBackend
    from names import Names

    sess = boto3.Session(region_name="ca-central-1")
    names = Names(sess=sess)
    return AthenaBackend(names, sess), names, sess, lake


def evidence(rid: str, command: list[str]) -> Path:
    folder = HERE / "evidence" / rid
    folder.mkdir(parents=True, exist_ok=True)
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE).stdout.strip()
    except OSError:
        sha = ""
    (folder / "command.txt").write_text(" ".join(command) + f"\ncommit {sha}\n")
    (folder / "summary.sql").write_bytes((HERE / "summary.sql").read_bytes())
    return folder


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["build", "run", "summary", "late", "clean"])
    p.add_argument("--offline", action="store_true")
    p.add_argument("--max-usd", type=float, default=None)
    p.add_argument("--copies", choices=["same", "dated"], default="same")
    p.add_argument("--max-copies", type=int, default=None,
                   help="the largest size the growth may plan (default 256; offline 4, so SIM01 gives 1, 2 and 4)")
    p.add_argument("--no-duckdb", action="store_true", help="Athena cells only")
    p.add_argument("--yes", action="store_true", help="clean: delete (without it, print what it would delete)")
    p.add_argument("--source", type=Path, help="offline: the Parquet file used as DS01 (default: SIM01)")
    args = p.parse_args(argv)
    argv_list = sys.argv if argv is None else ["experiment.py", *argv]
    results = LOCAL_ROOT / "results.duckdb" if args.offline else RESULTS
    results.parent.mkdir(parents=True, exist_ok=True)
    con = connect(results)
    load_prices(con)
    if args.offline:
        from backends import LocalBackend

        backend = LocalBackend(LOCAL_ROOT)
    elif args.step != "summary":
        backend, names, sess, lake = live_backend()
    if args.step == "build":
        source = args.source
        if args.offline and source is None:
            from sim_data import write_sim01

            source = write_sim01(LOCAL_ROOT / "sim01.parquet")
        if not args.offline:
            lake.up(sess)
            for wg in (names.ctas_wg, names.wg):
                print(f"workgroup {wg}: {lake.ensure_workgroup(wg, sess=sess)}")
            catalog = os.environ.get("COURSE_CATALOG_ID")
            if not catalog:
                raise SystemExit("set COURSE_CATALOG_ID to the course catalog's account id (tutorial B3T1)")
            backend.ensure_course_catalog(catalog)
            backend.check_source_columns()
        g = build(backend, max_usd(args.max_usd), dated=args.copies == "dated", source=source, con=con,
                  expect_rows=None if args.offline else DS01_ROWS,
                  max_copies=args.max_copies or (4 if args.offline else 256), state=state_path(args.offline))
        if not args.offline and g.sizes and not args.no_duckdb:
            import function

            zip_path = function.package(HERE / "build" / "duckdb-query.zip")
            function.deploy(names, sess, zip_path)
        return 0 if g.sizes else 1
    if args.step == "run":
        state = json.loads(state_path(args.offline).read_text())
        if not state["sizes"]:
            raise SystemExit(f"no plan: {state['note']}")
        evidence(run_id(), argv_list)
        run(backend, state["sizes"], con, duckdb_engine=not args.no_duckdb)
        return 0
    if args.step == "summary":
        sp = state_path(args.offline)
        note = json.loads(sp.read_text())["note"] if sp.exists() else ""
        print(write_summary(con, (LOCAL_ROOT if args.offline else HERE) / "summary.md", f"Growth: {note}."))
        return 0
    if args.step == "late":
        out = late(backend)
        print(json.dumps(out))
        return 0 if out["agree"] else 1
    if args.step == "clean":
        if args.offline:
            for w in backend.clean(args.yes):
                print(("removed " if args.yes else "would remove ") + w)
            if not args.yes:
                print("Nothing deleted. Run again with --yes to delete these.")
            return 0
        from clean_up import clean_up

        steps = clean_up(names, yes=args.yes, sess=sess)
        code = steps.report()
        if not args.yes:
            print("Nothing deleted. Run again with --yes to delete these.")
        return code
    return 0


assert MB and TB
if __name__ == "__main__":
    sys.exit(main())
