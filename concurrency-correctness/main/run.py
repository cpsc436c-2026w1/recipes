"""Main scenario: two drivers' apps accept the same trip at the same moment, with and without a conditional write.

    uv run python main/run.py --target local                   # moto, on your laptop, no AWS account
    PREFIX=recipe-ana uv run python main/run.py --target aws   # your team sandbox, ca-central-1

Creates the table $PREFIX-race-trips, runs --rounds races in each mode and prints how many rounds ended with one
winner and how many told both drivers they had the trip. The aws run removes the table at the end unless --keep.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]

import race  # noqa: E402
import trips  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ca-central-1")


def run_both(make_client, table: str, rounds: int) -> dict:
    trips.create_table(make_client(), table)
    return {mode: race.race_rounds(make_client, table, mode, rounds) for mode in ("unsafe", "safe")}


def show_one(make_client, table: str) -> str:
    """One safe race, written out: who was told what, and what the table holds."""
    r = race.race_once(make_client, table, "t-0042", "safe")
    lines = ["one race on trip t-0042, safe mode:"]
    for d, res in sorted(r["results"].items()):
        lines.append(f"  {d}: " + ("accepted" if res["accepted"] else f"refused, the trip is held by {res['held_by']}"))
    lines.append(f"  table: driver {r['stored_driver']}, version {r['version']}")
    return "\n".join(lines)


def report(out: dict) -> str:
    lines = [f"{'mode':8} {'rounds':>6} {'one winner':>10} {'both told yes':>13} {'winner is stored':>16}"]
    for mode, s in out.items():
        lines.append(f"{mode:8} {s['rounds']:>6} {s['one_winner']:>10} {s['both_told_yes']:>13} "
                     f"{s['winner_is_stored']:>16}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["local", "aws"], default="local")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--keep", action="store_true", help="aws only: keep the table (clean_up.py removes it)")
    a = ap.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    table = trips.table_name(prefix)
    if a.target == "local":
        from moto import mock_aws

        import local
        with mock_aws():
            make = local.client_factory(REGION)
            out = run_both(make, table, a.rounds)
            example = show_one(make, table)
    else:
        import boto3
        session = boto3.Session(region_name=REGION)
        # one session and client per thread: boto3 sessions are not safe to share between threads
        make = lambda: boto3.session.Session(region_name=REGION).client("dynamodb")   # noqa: E731
        run_failed = True
        try:
            out = run_both(make, table, a.rounds)
            example = show_one(make, table)
            run_failed = False
        finally:
            if not a.keep:
                import clean_up
                # clean() prints what is left. A run that worked then exits 1, so nobody reads it as done; a run
                # that failed keeps its own error.
                if clean_up.clean(session, prefix).failed and not run_failed:
                    sys.exit(1)
    print(f"target: {a.target}, table: {table}")
    print(report(out))
    print()
    print(example)
    return out


if __name__ == "__main__":
    main()
