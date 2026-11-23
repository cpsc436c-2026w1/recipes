"""Copy past trips into the lake beside the live ones, count them against the source, and load them into DynamoDB.

    uv run python ext-backfill/backfill.py --source course --from 2024-03-01 --to 2024-03-30      # offline (DuckDB)
    uv run python ext-backfill/backfill.py --source files --files raw/history/ --from 2024-03-14 --to 2024-03-15
    uv run python ext-backfill/backfill.py --check-only --from 2024-03-01 --to 2024-03-30         # counts only
    uv run python ext-backfill/backfill.py --view-only --live-from 2026-11-23T14:00:00Z           # view trips_all
    uv run python ext-backfill/backfill.py --to-dynamodb                                          # DynamoDB
    PREFIX=backfill-yourname uv run python ext-backfill/backfill.py --target aws --source course --from 2024-03-01 --to 2024-03-30

The copy reads the days already in trips_history and their counts, copies the missing days (one CTAS the first
time, one INSERT INTO for each further month), deletes and copies again a day whose count differs from the
source's, then compares every day's count with the source's and exits 1 if one differs. It ends with the view
trips_all over the history and the live table trips, when trips exists or --live-from is given.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for folder in (HERE, HERE.parent / "scripts", HERE.parent):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import statements as sql  # noqa: E402
from engines import AthenaEngine, LocalEngine  # noqa: E402
from skeleton.config import names, prefix  # noqa: E402

CATALOG_ENV = "COURSE_CATALOG_ID"     # the course catalog's account id, as tutorial B3T1's setup_athena.sh gives it


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def parse_time(text: str) -> datetime:
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--source", choices=["course", "files"], default="course",
                   help="course: the course catalog's course_rides.rides; files: your own files, as history.toml says")
    p.add_argument("--files", help="with --source files: the folder (offline) or s3:// prefix of your files")
    p.add_argument("--history-toml", default=str(HERE / "history.toml"))
    p.add_argument("--from", dest="first", type=date.fromisoformat)
    p.add_argument("--to", dest="last", type=date.fromisoformat)
    p.add_argument("--run-id", help="the backfill's run_id on every row (default bf-<UTC time>-<4 hex digits>)")
    p.add_argument("--check-only", action="store_true", help="only compare the counts per day")
    p.add_argument("--replace-source", action="store_true",
                   help="copy from this source although trips_history was copied from another (its differing days "
                        "are deleted and copied again)")
    p.add_argument("--view-only", action="store_true", help="only (re)create the view trips_all")
    p.add_argument("--live-from", help="the cutover time (default: the earliest requested_time in trips)")
    p.add_argument("--to-dynamodb", action="store_true", help=f"load trips_history into <PREFIX>-trips-by-id")
    p.add_argument("--import-format", choices=["dynamodb_json", "csv"], default="dynamodb_json")
    p.add_argument("--load", choices=["auto", "batch"], default="auto",
                   help="batch: BatchWriteItem into a table the script makes, where ImportTable is refused")
    p.add_argument("--load-id", help="the DynamoDB load's own id on every item (default: the run id)")
    p.add_argument("--sample", default=os.environ.get("BACKFILL_SAMPLE"),
                   help="offline: the CSV that stands in for the course table (default sim/SIM01-march-sample.csv)")
    return p


def make_engine(args, db: str):
    wg, ctas_wg = f"{prefix()}-wg", f"{prefix()}-ctas-wg"
    if args.target == "local":
        return LocalEngine(db, wg, ctas_wg, sample=args.sample, run_id=args.run_id), None, None
    import boto3
    from lake import WorkgroupMismatch, bucket_name, ensure_workgroup, up
    from tags import record

    sess = boto3.session.Session(region_name="ca-central-1")
    up(sess)
    for name in (wg, ctas_wg):
        try:
            print(f"{ensure_workgroup(name, sess=sess):<8} workgroup {name}")
        except WorkgroupMismatch as e:
            raise SystemExit(f"{e}.\nA workgroup of this name made by another script is never changed: delete it, "
                             f"or use another PREFIX. A new PREFIX is the safer choice: the project-wide clean_up.py "
                             f"would delete the bucket and database too.") from e
    glue = sess.client("glue")
    try:
        glue.get_database(Name=db)
        from tags import recorded
        if recorded("glue-database", db) is None:
            record("glue-database", db, "borrowed", script="ext-backfill/backfill.py")
    except glue.exceptions.EntityNotFoundException:
        from tags import tags
        glue.create_database(DatabaseInput={"Name": db}, Tags=tags("glue"))
        record("glue-database", db, "created", script="ext-backfill/backfill.py", clean="ext-backfill/clean_up.py --yes")
    return AthenaEngine(db, wg, ctas_wg, sess, bucket_name(sess)), sess, bucket_name(sess)


def ensure_course_source(sess) -> None:
    """The data source `course` over the course catalog, registered if missing; recorded borrowed, never deleted."""
    from tags import record, recorded

    athena = sess.client("athena")
    try:
        athena.get_data_catalog(Name="course")
    except athena.exceptions.ClientError as e:
        if "not found" not in str(e).lower() and "InvalidRequest" not in str(e):
            raise
        catalog = os.environ.get(CATALOG_ENV)
        if not catalog:
            raise SystemExit(f"Athena has no data source 'course' yet. Set {CATALOG_ENV} to the course catalog's "
                             f"account id (tutorial B3T1, setup_athena.sh) and run again.") from e
        athena.create_data_catalog(Name="course", Type="GLUE", Parameters={"catalog-id": catalog},
                                   Description="the course catalog (read only)")
        print("registered Athena data source course")
    if recorded("athena-data-catalog", "course") is None:
        record("athena-data-catalog", "course", "borrowed", script="ext-backfill/backfill.py --source course",
               clean="never deleted: the course's")


def source_id(src, files: str | None) -> str:
    """What trips_history is copied from: `course`, or `files:` and the folder or prefix of the files."""
    return "course" if src.kind == "course" else f"files:{(files or '').rstrip('/')}/"


def copy_history(engine, src, days: list[date], run_id: str, log=print, source: str | None = None,
                 replace_source: bool = False) -> dict[date, int]:
    """Copy the days that are missing or short; returns the source's count per day. A trips_history copied from
    another source is not changed (a day whose count differs would be deleted and copied from this one) unless
    replace_source is given."""
    exists = engine.table_exists("trips_history")
    before = engine.history_source() if exists and source else None
    if before and before != source and not replace_source:
        raise SystemExit(f"trips_history was copied from {before}, not {source}: a day whose count differs would be "
                         f"deleted and copied from the new source. Use another PREFIX, or run "
                         f"ext-backfill/clean_up.py --yes first, or pass --replace-source to replace those days.")
    want = engine.count(src, days)
    have = engine.history_counts(days) if exists else {}
    short = [d for d in days if d in have and have[d] != want.get(d, 0)]
    missing = [d for d in days if d not in have and want.get(d, 0) > 0]
    kept = len(days) - len(short) - len(missing)
    log(f"days     {len(days)} asked for: {kept} already complete, {len(missing)} missing, {len(short)} short")
    for d in short:
        gone = engine.delete_day(d)
        log(f"deleted  requested_day={d}: {have[d]} rows (the source has {want.get(d, 0)}), "
            f"{plural(len(gone), 'file')} and its partition")
    if not exists and getattr(engine, "leftover_files", lambda: 0)():
        raise SystemExit("trips_history is not in the Glue catalog, and files are left under history/trips/ "
                         "(a CTAS writes only to an empty location): run ext-backfill/clean_up.py --yes first")
    for month in sql.months(short + missing):
        first = not exists
        engine.copy(src, month, run_id, first=first)
        exists = True
        log(f"{'ctas' if first else 'insert':<8} {month[0]} to {month[-1]}: {plural(len(month), 'day')}")
    if source and exists:
        engine.set_history_source(source)
    return want


def check_counts(engine, days: list[date], want: dict[date, int], log=print) -> int:
    have = engine.history_counts(days)
    differ = [d for d in days if have.get(d, 0) != want.get(d, 0)]
    for d in differ:
        log(f"differ   {d}: {have.get(d, 0)} rows, the source has {want.get(d, 0)}")
    log(f"counts   {len(days)} days, {len(differ)} differ; {sum(have.values())} rows in trips_history, "
        f"{sum(want.get(d, 0) for d in days)} in the source")
    return 1 if differ else 0


def make_view(engine, live_from: str | None, log=print) -> None:
    cutover = parse_time(live_from) if live_from else engine.earliest_live()
    if cutover is None:
        log(f"view     not made: no live table {engine.db}.trips yet and no --live-from")
        return
    n = engine.make_view(cutover)
    log(f"view     {engine.db}.trips_all: history before {cutover:%Y-%m-%dT%H:%M:%SZ}, live trips from it")
    log(f"view     {plural(n, 'history row')} at or after the cutover left out")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    db = names()["database"]
    args.run_id = run_id = args.run_id or (datetime.now(timezone.utc).strftime("bf-%Y%m%dT%H%M%SZ-")
                                              + secrets.token_hex(2))
    engine, sess, bucket = make_engine(args, db)
    try:
        return run(args, engine, sess, bucket, run_id)
    finally:
        engine.close()


def run(args, engine, sess, bucket, run_id: str) -> int:
    days = days_between(args.first, args.last) if args.first and args.last else []
    if args.to_dynamodb:
        import dynamo_load
        rows = engine.history_rows(days or None)
        print(f"read     {len(rows)} rows of trips_history")
        if not rows:
            raise SystemExit("trips_history has no rows" + (" for those days" if days else "") + ": copy the history "
                             "first (backfill.py --source course or --source files, with --from and --to)")
        if sess is not None:
            return dynamo_load.load(sess, f"{prefix()}-trips-by-id", bucket, rows, args.load_id or run_id,
                                    fmt=args.import_format, mode=args.load)
        import boto3
        from lake import bucket_name, up
        from skeleton.offline import fake_aws
        with fake_aws():                     # offline: moto plays DynamoDB and S3 in this process
            sess = boto3.session.Session(region_name="ca-central-1")
            up(sess, log=lambda *_: None)
            return dynamo_load.load(sess, f"{prefix()}-trips-by-id", bucket_name(sess), rows,
                                    args.load_id or run_id, fmt=args.import_format, mode=args.load)
    if args.view_only:
        make_view(engine, args.live_from)
        return 0
    if not days:
        raise SystemExit("give the days with --from YYYY-MM-DD --to YYYY-MM-DD")
    src = sql.course_source() if args.source == "course" else sql.files_source(Path(args.history_toml))
    if src.kind == "files" and not args.files:
        raise SystemExit("--source files needs --files <folder or s3:// prefix of your files>")
    if src.kind == "course" and sess is not None:
        ensure_course_source(sess)
    engine.ensure_source(src, args.files)
    if args.check_only:
        code = check_counts(engine, days, engine.count(src, days))
    else:
        print(f"run      {run_id}: {src.kind} source, {days[0]} to {days[-1]}")
        want = copy_history(engine, src, days, run_id, source=source_id(src, args.files),
                            replace_source=args.replace_source)
        code = check_counts(engine, days, want)
        make_view(engine, args.live_from)
    if engine.scanned:
        print(f"scanned  {engine.scanned:,} bytes in Athena in this run")
    return code


if __name__ == "__main__":
    sys.exit(main())
