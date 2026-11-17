"""Main scenario: live updates -> Kinesis Data Streams -> Lambda consumer -> DynamoDB, with the lag of the run.

    uv run python main/run.py --target local                 # moto, on your laptop, no AWS account
    PREFIX=recipe-ana uv run python main/run.py --target aws # your team sandbox, ca-central-1

With --target aws the run creates the stream, table, queue, role, function and mapping, sends the updates at --rate a
second, waits until all are stored, prints the lag and removes everything (unless --keep).
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import consumer  # noqa: E402
import infra  # noqa: E402
import lag  # noqa: E402
import producer  # noqa: E402
from local_poller import drain  # noqa: E402
from updates import make_updates, read_jsonl  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ca-central-1")


def run_local(n: int, prefix: str, updates=None) -> dict:
    """Whole scenario against moto: the same boto3 calls, with local_poller standing in for Lambda's poller."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        s = boto3.Session(region_name=REGION)
        names = infra.names(prefix)
        kinesis, ddb = s.client("kinesis"), s.client("dynamodb")
        infra.create_stream(kinesis, names["stream"])
        infra.create_table(ddb, names["table"], "update_id")
        updates = updates or make_updates(n)
        sent = producer.send(kinesis, names["stream"], updates)
        polled = drain(kinesis, names["stream"], lambda ev: consumer.handler(ev, ddb=ddb, table=names["table"]))
        items = lag.scan_all(ddb, names["table"])
        return {"sent": sent, "polled": polled, "expected": len({u["update_id"] for u in updates}),
                "summary": lag.summarize(items)}


def wait_stored(ddb, table: str, expected: int, timeout_s: int) -> int:
    end = time.time() + timeout_s
    count = 0
    while time.time() < end:
        count = ddb.scan(TableName=table, Select="COUNT", ConsistentRead=True)["Count"]
        if count >= expected:
            break
        time.sleep(5)
    return count


def run_aws(n: int, prefix: str, rate: float, wait_s: int, keep: bool, updates=None) -> dict:
    import boto3

    s = boto3.Session(region_name=REGION)
    names = infra.names(prefix)
    print(f"creating {names['stream']}, {names['table']}, {names['consumer']} in {REGION} ...")
    lam, ddb = s.client("lambda"), s.client("dynamodb")
    run_failed = True
    try:                                        # the deploy is inside: a run that stops half-way is cleaned up too
        made = infra.deploy_main(s, prefix)
        infra.wait_mapping(lam, made["mapping"])
        updates = updates or make_updates(n)
        sent = producer.send(s.client("kinesis"), names["stream"], updates, rate=rate)
        print(f"sent {sent['sent']} updates in {sent['seconds']} s; waiting for the consumer ...")
        expected = len({u["update_id"] for u in updates})   # a file may hold an update twice; it is stored once
        stored = wait_stored(ddb, names["table"], expected, wait_s)
        if stored < expected:
            print(f"only {stored} of {expected} stored after {wait_s} s: check the function's logs and the "
                  f"queue {names['consumer_failures']}")
        items = lag.scan_all(ddb, names["table"])
        out = {"sent": sent, "expected": expected, "summary": lag.summarize(items)}
        run_failed = False
        return out
    finally:
        if not keep:
            import clean_up
            # clean() prints what is left. A run that worked then exits 1, so nobody reads it as done; a run that
            # failed keeps its own error.
            if clean_up.clean(s, prefix).failed and not run_failed:
                sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["local", "aws"], default="local")
    ap.add_argument("--updates", type=int, default=300, help="how many updates to send (default 300)")
    ap.add_argument("--rate", type=float, default=10, help="aws only: updates a second (default 10)")
    ap.add_argument("--from-file", help="send the updates in this JSON-lines file instead of generated ones")
    ap.add_argument("--wait", type=int, default=180, help="aws only: seconds to wait for every update to be stored")
    ap.add_argument("--keep", action="store_true", help="aws only: keep the resources (clean_up.py removes them)")
    args = ap.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    updates = read_jsonl(args.from_file) if args.from_file else None
    if args.target == "local":
        out = run_local(args.updates, prefix, updates)
    else:
        out = run_aws(args.updates, prefix, args.rate, args.wait, args.keep, updates)
    print(f"target: {args.target}, prefix: {prefix}, sent: {out['sent']['sent']}, "
          f"retried by the producer: {out['sent']['retried']}")
    print(lag.report(out["summary"]))
    if out["summary"]["stored"] < out["expected"]:
        # some updates were sent and not stored: the report above is of the rest, and the run did not do its job
        sys.exit(1)
    return out


if __name__ == "__main__":
    main()
