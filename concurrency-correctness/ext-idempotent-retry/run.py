"""Extension: a tip whose first write succeeds but whose reply is lost; the app retries with the same request id.

    uv run python ext-idempotent-retry/run.py --target local
    PREFIX=recipe-ana uv run python ext-idempotent-retry/run.py --target aws

Runs the same tap twice, once with the naive write and once with the request id, each on a fresh trip, and prints the
tip the table holds after the retry. The reply is lost by ReplyLostOnce (tips.py), on purpose, in both targets.
"""

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent / "main"), str(HERE.parent)]

import tips  # noqa: E402
import trips  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ca-central-1")
TIP = 5


def scenario(ddb, prefix: str) -> dict:
    table, requests = trips.table_name(prefix), tips.requests_table_name(prefix)
    trips.create_table(ddb, table)
    tips.create_requests_table(ddb, requests)
    out = {}
    for mode in ("naive", "request id"):
        trip_id = "t-tip-naive" if mode == "naive" else "t-tip-once"
        trips.reset_trip(ddb, table, trip_id)
        flaky = tips.ReplyLostOnce(ddb)
        request_id = tips.new_request_id()                       # one tap, one id, reused on every attempt
        sends = "no request id" if mode == "naive" else f"request id {request_id}"
        print(f"{mode}: rider taps 'add {TIP} tip' on {trip_id}, the app sends it with {sends}")
        if mode == "naive":
            call = lambda: tips.add_tip_naive(flaky, table, trip_id, TIP)            # noqa: E731
        else:
            call = lambda: tips.add_tip_once(flaky, table, trip_id, TIP, request_id, requests)   # noqa: E731
        tips.send_with_retries(call)
        out[mode] = trips.read_trip(ddb, table, trip_id)["tip_total"]
        print(f"  tip on the trip now: {out[mode]} (the rider asked for {TIP})\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["local", "aws"], default="local")
    ap.add_argument("--keep", action="store_true", help="aws only: keep the tables (clean_up.py removes them)")
    a = ap.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    import boto3
    if a.target == "local":
        from moto import mock_aws
        with mock_aws():
            out = scenario(boto3.client("dynamodb", region_name=REGION), prefix)
    else:
        session = boto3.Session(region_name=REGION)
        run_failed = True
        try:
            out = scenario(session.client("dynamodb"), prefix)
            run_failed = False
        finally:
            if not a.keep:
                import clean_up
                # clean() prints what is left. A run that worked then exits 1, so nobody reads it as done; a run
                # that failed keeps its own error.
                if clean_up.clean(session, prefix).failed and not run_failed:
                    sys.exit(1)
    print(f"target: {a.target}; tip after one tap and one retry: naive {out['naive']}, "
          f"with request id {out['request id']}")
    return out


if __name__ == "__main__":
    main()
