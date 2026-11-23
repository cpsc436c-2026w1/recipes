"""The live run of ext-scheduled-merge: SIM01 into S3, one merge run, the tables checked against the answer
from _truth/ (sim/truth.py).

    export PREFIX=recipe-yourname
    uv run python scripts/lake.py up
    uv run python ext-scheduled-merge/merge.py --target aws up
    uv run python ext-scheduled-merge/live_run.py                  # about 8 minutes; writes .local/ext-scheduled-merge.txt
    uv run python ext-scheduled-merge/clean_up.py --yes

It replays SIM01 through the replayer's s3 target with its times moved to now (so the run's K + 1 open days hold
them) and a run id in front of every id, waits until the last object has settled (SETTLE_S, 5 minutes, plus a
minute), invokes the function once, and then reads, through Athena in <PREFIX>-wg:
  - the trips of this replay per zone (trip ids that start with the run id), against that answer
  - zone_counts of the replay's day against it (the counts also hold any earlier replay of that day, so
    run it on a day without one)
  - Data scanned and engine time of the run's MERGE and rebuild, and how many objects the MERGE read
The account number is masked in what it writes. A second function run (`merge.py --target aws run-now`) must leave
the trips unchanged (R100).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(ROOT / "scripts"), str(ROOT / "sim"), str(ROOT)] if p not in sys.path]

import merge  # noqa: E402
import merge_run  # noqa: E402

REGION = "ca-central-1"
MASK = "111122223333"


def query(athena, sql: str, workgroup: str, database: str) -> tuple[list[dict], dict]:
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup,
                                       QueryExecutionContext={"Database": database})["QueryExecutionId"]
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if q["Status"]["State"] != "SUCCEEDED":
        raise RuntimeError(f"{q['Status']['State']}: {q['Status'].get('StateChangeReason')}")
    rows, header = [], None
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for r in page["ResultSet"]["Rows"]:
            vals = [c.get("VarCharValue") for c in r["Data"]]
            if header is None:
                header = vals
            else:
                rows.append(dict(zip(header, vals)))
    return rows, q


def stats(athena, qid: str) -> str:
    s = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"].get("Statistics", {})
    return f"{s.get('DataScannedInBytes', 0)} B Data scanned, {s.get('EngineExecutionTimeInMillis', 0)} ms"


def main(argv=None) -> int:
    import boto3
    import lake
    import replay
    import sim_data

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settle-s", type=int, default=360, help="wait after the last object (default 360)")
    p.add_argument("--out", default=str(ROOT / ".local" / "ext-scheduled-merge.txt"),
                   help="where the run is written (default .local/; output/ holds the recorded run)")
    args = p.parse_args(argv)
    sess = boto3.session.Session(region_name=REGION)
    account, n = lake.account_id(sess), merge.names()
    lines: list[str] = []

    def log(text: str) -> None:
        text = text.replace(account, MASK)
        print(text)
        lines.append(text)

    run_id = "live" + datetime.now(timezone.utc).strftime("%H%M")
    deliveries, truth = sim_data.load("SIM01")
    sender = replay.make_sender("s3", run_id=run_id, session=sess)
    from tags import record                 # scripts/lake.py down deletes the slot files of the run ids recorded here
    record("s3-replay", run_id, "created", script="ext-scheduled-merge/live_run.py", clean="scripts/lake.py down")
    t0 = time.monotonic()
    summary = replay.replay(deliveries, sender, run_id, shift_to=datetime.now(timezone.utc))
    log(f"replayed SIM01 as {run_id}: {summary['sent']} deliveries in {len(sender.keys)} objects, "
        f"{summary['failed']} failed, {time.monotonic() - t0:.1f} s")
    log(f"waiting {args.settle_s} s for the objects to settle")
    time.sleep(args.settle_s)
    t1 = time.monotonic()
    body = merge.run_now(sess, log=lambda *_: None)
    log(f"function run: {body.get('run_state')} in {time.monotonic() - t1:.1f} s, objects {body.get('objects')}, "
        f"quarantined {body.get('quarantined')}, old_events {body.get('old_events')}")
    athena = sess.client("athena")
    for step in ("merge", "rebuild"):
        qid = body.get(f"{step}_query_id")
        if qid:
            log(f"{step:<8} {qid}: {stats(athena, qid)}")
    rows, q = query(athena, f"SELECT pickup_zone_id, count(*) AS trips FROM trips WHERE trip_id LIKE '{run_id}-%' "
                            f"GROUP BY 1 ORDER BY 1", n["workgroup"], n["database"])
    got = {r["pickup_zone_id"]: int(r["trips"]) for r in rows}
    want = truth["requested_per_zone"]
    log(f"trips of {run_id} per zone: {json.dumps(got, sort_keys=True)}; truth: {json.dumps(want, sort_keys=True)}")
    day = parse_day(sender.keys[0])
    rows, _ = query(athena, f"SELECT pickup_zone_id, trips_requested, trips_completed FROM zone_counts "
                            f"WHERE period_start = DATE '{day}' ORDER BY 1", n["workgroup"], n["database"])
    log(f"zone_counts {day}: " + ", ".join(f"{r['pickup_zone_id']} {r['trips_requested']} {r['trips_completed']}"
                                           for r in rows))
    counted = {r["pickup_zone_id"]: int(r["trips_requested"]) for r in rows}
    completed = sum(int(r["trips_completed"]) for r in rows)
    log(f"zone_counts {day} requested per zone {json.dumps(counted, sort_keys=True)}, completed {completed}; "
        f"truth: {json.dumps(want, sort_keys=True)}, completed {truth['completed']}")
    s3 = sess.client("s3")
    sizes = {o["Key"]: o["Size"] for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=sender.bucket, Prefix=f"raw/trip-events/dt={day}/") for o in page.get("Contents", [])}
    log(f"objects in the day folder: {len(sizes)}, {sum(sizes.values())} B; taken by the run: {body.get('objects')}, "
        f"{sum(sizes.get(k, 0) for k in sender.keys)} B (the MERGE's Data scanned is these bytes and trips' when "
        f"\"$path\" limits the read)")
    ok = (body.get("run_state") == "finished" and all(got.get(z) == c for z, c in want.items())
          and all(counted.get(z) == c for z, c in want.items()) and completed == truth["completed"])
    log("result: trips and zone_counts match truth" if ok else "result: MISMATCH (see above)")
    Path(args.out).write_text("\n".join(lines) + "\n")
    return 0 if ok else 1


def parse_day(key: str) -> str:
    return re.search(r"dt=(\d{4}-\d{2}-\d{2})/", key).group(1)


if __name__ == "__main__":
    sys.exit(main())
