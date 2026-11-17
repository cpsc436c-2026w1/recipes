"""Extension: counts per zone in 5-minute tumbling windows by event time, with a rule for late records.

    uv run python ext-windowed-aggregates/run.py --target local
    uv run python ext-windowed-aggregates/run.py --target local --late-rule correct
    PREFIX=recipe-ana uv run python ext-windowed-aggregates/run.py --target aws

What changes from main/: a second consumer (windows.py) reads the same stream into its own table, and the producer
sends a simulated half hour with fixed event times (timeline.py) instead of updates stamped at send time.
"""

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent / "main"), str(HERE.parent)]

import infra  # noqa: E402
import producer  # noqa: E402
import windows  # noqa: E402
from local_poller import drain  # noqa: E402
from timeline import make_timeline, truth  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ca-central-1")


def compare(rows, newest_ms, close_delay_s, expected) -> list[dict]:
    """Each window beside its true count, marked final once the watermark has passed its end."""
    watermark = None if newest_ms is None else newest_ms - close_delay_s * 1000
    out = []
    for w in rows:
        end_ms = windows._ms(w["window_end"])
        out.append({**w, "truth": expected.get((w["zone_id"], w["window_start"]), 0),
                    "final": watermark is not None and end_ms <= watermark})
    return out


def report(rows, rule) -> str:
    lines = [f"{'zone':>4}  {'window start (UTC)':20} {'count':>5} {'late dropped':>12} {'corrections':>11} "
             f"{'truth':>5}  state"]
    for r in rows:
        lines.append(f"{r['zone_id']:>4}  {r['window_start']:20} {r['count']:>5} {r['late_dropped']:>12} "
                     f"{r['corrections']:>11} {r['truth']:>5}  {'final' if r['final'] else 'open'}")
    total = {k: sum(r[k] for r in rows) for k in ("count", "late_dropped", "corrections", "truth")}
    lines.append(f"\ntotal: counted {total['count']}, late and dropped {total['late_dropped']}, "
                 f"late and corrected {total['corrections']}, truth {total['truth']} (rule: {rule})")
    held = "count + late dropped" if rule == "drop" else "count"
    ok = all((r["count"] + (r["late_dropped"] if rule == "drop" else 0)) == r["truth"] for r in rows)
    lines.append(f"every window: {held} = truth: {'yes' if ok else 'NO'}")
    return "\n".join(lines)


def run_local(rule: str, close_delay_s: int, batch: int, prefix: str) -> list[dict]:
    import boto3
    from moto import mock_aws

    with mock_aws():
        s = boto3.Session(region_name=REGION)
        n = infra.names(prefix)
        kinesis, ddb = s.client("kinesis"), s.client("dynamodb")
        infra.create_stream(kinesis, n["stream"])
        infra.create_table(ddb, n["windows_table"], "pk")
        updates = make_timeline()
        producer.send(kinesis, n["stream"], updates)
        drain(kinesis, n["stream"], batch_size=batch, handler=lambda ev: windows.handler(
            ev, ddb=ddb, table=n["windows_table"], rule=rule, close_delay_s=close_delay_s))
        rows, newest = windows.read_windows(ddb, n["windows_table"])
        return compare(rows, newest, close_delay_s, truth(updates))


def deploy(session, prefix: str, rule: str, close_delay_s: int, batch: int) -> str:
    n = infra.names(prefix)
    region, acct = session.region_name, infra.account_id(session)
    kinesis, ddb, sqs = session.client("kinesis"), session.client("dynamodb"), session.client("sqs")
    iam, lam = session.client("iam"), session.client("lambda")
    stream_arn = infra.create_stream(kinesis, n["stream"])
    table_arn = infra.create_table(ddb, n["windows_table"], "pk")
    infra.expire_items(ddb, n["windows_table"], "expires_at")
    _, failures_arn = infra.create_queue(sqs, n["windows_failures"], keep_days=infra.FAILED_KEEP_DAYS)
    role = infra.create_role(iam, f"{n['windows']}-role", [
        {"Effect": "Allow", "Action": infra.KINESIS_READ, "Resource": stream_arn},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem",
                                       "dynamodb:ConditionCheckItem"], "Resource": table_arn},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": failures_arn},
    ], region, acct, n["windows"])
    infra.create_log_group(session.client("logs"), n["windows"])
    infra.create_function(lam, n["windows"], role, infra.zip_files({"windows.py": HERE / "windows.py"}),
                          "windows.handler", {"TABLE": n["windows_table"], "LATE_RULE": rule,
                                              "CLOSE_DELAY_S": str(close_delay_s)})
    return infra.kinesis_mapping(lam, n["windows"], stream_arn, failures_arn, batch_size=batch)


def run_aws(rule, close_delay_s, batch, prefix, rate, wait_s, keep) -> list[dict]:
    import boto3

    s = boto3.Session(region_name=REGION)
    n = infra.names(prefix)
    lam, ddb = s.client("lambda"), s.client("dynamodb")
    run_failed = True
    try:
        uuid = deploy(s, prefix, rule, close_delay_s, batch)
        infra.wait_mapping(lam, uuid)
        updates = make_timeline(start_ms=int(time.time() * 1000) - 30 * 60_000)
        producer.send(s.client("kinesis"), n["stream"], updates, rate=rate)
        end = time.time() + wait_s
        while time.time() < end:
            rows, newest = windows.read_windows(ddb, n["windows_table"])
            if sum(r["count"] + r["late_dropped"] for r in rows) >= len(updates):
                break
            time.sleep(5)
        rows, newest = windows.read_windows(ddb, n["windows_table"])
        out = compare(rows, newest, close_delay_s, truth(updates))
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
    ap.add_argument("--late-rule", choices=["drop", "correct"], default="drop")
    ap.add_argument("--close-delay", type=int, default=120, help="seconds to wait after a window's end (default 120)")
    ap.add_argument("--batch", type=int, default=25, help="records per consumer call (default 25)")
    ap.add_argument("--rate", type=float, default=20, help="aws only: updates a second (default 20)")
    ap.add_argument("--wait", type=int, default=180)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    if a.target == "local":
        rows = run_local(a.late_rule, a.close_delay, a.batch, prefix)
    else:
        rows = run_aws(a.late_rule, a.close_delay, a.batch, prefix, a.rate, a.wait, a.keep)
    print(f"target: {a.target}, close delay: {a.close_delay} s, late rule: {a.late_rule}")
    print(report(rows, a.late_rule))
    return rows


if __name__ == "__main__":
    main()
