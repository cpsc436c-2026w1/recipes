"""One demonstration run: SIM01 to the stream at 10 times its speed (about 14 minutes) and to the lake bucket, then
one Athena count over raw/trip-events/; the query id goes to evidence/<run-id>/queries.json for scripts/units.py.

    PREFIX=cost-yourname uv run python scripts/demo_run.py --run-id run-1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_up import names  # noqa: E402
from lake import bucket_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = Path(os.environ.get("EVIDENCE") or ROOT / "evidence")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--speed", type=float, default=10)
    args = ap.parse_args(argv)
    sess = boto3.session.Session(region_name="ca-central-1")
    n, ev = names(), EVIDENCE / args.run_id
    if not (ROOT / "sim" / "data" / "SIM01" / "_truth" / "events.jsonl").exists():
        subprocess.run([sys.executable, str(ROOT / "sim" / "generate.py"), "SIM01"], check=True)
    py = [sys.executable, str(ROOT / "sim" / "replay.py"), "SIM01", "--run-id", args.run_id, "--shift-to", "now"]
    subprocess.run(py + ["--to", "kinesis", "--target", n["stream"], "--speed", str(args.speed),
                         "--evidence", str(ev)], check=True)
    subprocess.run(py + ["--to", "s3", "--target", bucket_name(sess), "--evidence", str(ev / "s3")], check=True)
    from tags import record                 # scripts/lake.py down deletes the slot files of the run ids recorded here
    record("s3-replay", args.run_id, "created", script="scripts/demo_run.py", clean="scripts/lake.py down")
    athena = sess.client("athena")
    qid = athena.start_query_execution(
        QueryString=f"SELECT count(*) AS deliveries FROM {n['database']}.{n['glue_table']}",
        WorkGroup=n["workgroup"])["QueryExecutionId"]
    while (state := athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]) in (
            "QUEUED", "RUNNING"):
        time.sleep(2)
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"] if state == "SUCCEEDED" else []
    print(f"athena   {state}: {rows[1]['Data'][0].get('VarCharValue') if len(rows) > 1 else '-'} deliveries in the lake")
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "queries.json").write_text(json.dumps([qid]))
    time.sleep(60)          # the function reads the stream about once a second; let it finish the last batches
    return 0 if state == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
