"""Extension: each update goes to an SNS topic; two consumers each read their own SQS queue.

    uv run python ext-fan-out/run.py --target local
    PREFIX=recipe-ana uv run python ext-fan-out/run.py --target aws

The dashboard consumer runs the whole time. The history consumer is stopped while the updates are sent and started
--stopped-for seconds later. The run shows that the dashboard received every update while history was stopped, that
history's queue held the updates meanwhile, and that history caught up once started.

What changes from main/: the producer publishes to a topic instead of putting records on the Kinesis stream, and each
consumer is a Lambda function on its own queue. Use main/ when one consumer must read in order per key or replay
old updates; use this when several independent consumers each need every update.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent / "main"), str(HERE.parent)]

import fanout_consumer  # noqa: E402
import infra  # noqa: E402
import lag  # noqa: E402
from updates import make_updates, stamp  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ca-central-1")
CONSUMERS = ("dashboard", "history")


def publish(sns, topic_arn: str, updates: list[dict], rate: float = 0.0) -> int:
    """PublishBatch, 10 messages per call (the SNS limit). Each update is stamped when it is sent."""
    start, sent = time.time(), 0
    for i in range(0, len(updates), 10):
        if rate:
            pause = start + i / rate - time.time()
            if pause > 0:
                time.sleep(pause)
        now_ms = int(time.time() * 1000)
        batch = [stamp(u, now_ms) for u in updates[i:i + 10]]
        resp = sns.publish_batch(TopicArn=topic_arn, PublishBatchRequestEntries=[
            {"Id": str(j), "Message": json.dumps(r)} for j, r in enumerate(batch)])
        if resp.get("Failed"):
            raise RuntimeError(f"publish failed: {resp['Failed']}")
        sent += len(batch)
    return sent


def make_topic_and_queues(session, prefix: str) -> dict:
    """Topic, one queue (with a dead-letter queue) per consumer, each queue subscribed with raw delivery."""
    n = infra.names(prefix)
    sns, sqs = session.client("sns"), session.client("sqs")
    topic_arn = sns.create_topic(Name=n["topic"], Tags=[{"Key": k, "Value": v} for k, v in infra.TAGS.items()])[
        "TopicArn"]
    out = {"topic_arn": topic_arn}
    for c in CONSUMERS:
        _, dlq_arn = infra.create_queue(sqs, f"{n[c]}-dlq", keep_days=infra.FAILED_KEEP_DAYS)
        url, arn = infra.create_queue(sqs, n[c], dead_letter_arn=dlq_arn)
        sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "sns.amazonaws.com"}, "Action": "sqs:SendMessage",
                "Resource": arn, "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}}}]})})
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=arn,
                      Attributes={"RawMessageDelivery": "true"})
        out[c] = {"url": url, "arn": arn}
    return out


def queue_depth(sqs, url: str) -> int:
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                               "ApproximateNumberOfMessagesNotVisible"])
    return int(a["Attributes"]["ApproximateNumberOfMessages"]) + int(a["Attributes"]["ApproximateNumberOfMessagesNotVisible"])


def poll_queue(sqs, url: str, handler) -> int:
    """Local stand-in for Lambda's SQS poller: receive up to 10, call the handler, delete what it did not report."""
    handled = 0
    while True:
        msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])
        if not msgs:
            return handled
        event = {"Records": [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
                              "attributes": m["Attributes"], "eventSource": "aws:sqs"} for m in msgs]}
        failed = {f["itemIdentifier"] for f in handler(event)["batchItemFailures"]}
        for m in msgs:
            if m["MessageId"] not in failed:
                sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
                handled += 1


def run_local(n_updates: int, stopped_for: float, prefix: str) -> dict:
    import boto3
    from moto import mock_aws

    with mock_aws():
        s = boto3.Session(region_name=REGION)
        n = infra.names(prefix)
        ddb, sqs = s.client("dynamodb"), s.client("sqs")
        made = make_topic_and_queues(s, prefix)
        for c in CONSUMERS:
            infra.create_table(ddb, n[c], "update_id")
        updates = make_updates(n_updates)
        sent = publish(s.client("sns"), made["topic_arn"], updates)
        fn = {c: (lambda ev, t=n[c]: fanout_consumer.handler(ev, ddb=ddb, table=t)) for c in CONSUMERS}
        poll_queue(sqs, made["dashboard"]["url"], fn["dashboard"])          # dashboard runs; history is stopped
        while_stopped = {"dashboard stored": len(lag.scan_all(ddb, n["dashboard"])),
                         "history stored": len(lag.scan_all(ddb, n["history"])),
                         "history queue holds": queue_depth(sqs, made["history"]["url"])}
        time.sleep(stopped_for)
        poll_queue(sqs, made["history"]["url"], fn["history"])              # history starts and catches up
        return {"sent": sent, "while_stopped": while_stopped,
                "summary": {c: lag.summarize(lag.scan_all(ddb, n[c])) for c in CONSUMERS}}


def wait_count(ddb, table, expected, wait_s) -> int:
    end, count = time.time() + wait_s, 0
    while time.time() < end:
        count = ddb.scan(TableName=table, Select="COUNT", ConsistentRead=True)["Count"]
        if count >= expected:
            break
        time.sleep(5)
    return count


def run_aws(n_updates, stopped_for, prefix, rate, wait_s, keep) -> dict:
    import boto3

    s = boto3.Session(region_name=REGION)
    n = infra.names(prefix)
    region, acct = s.region_name, infra.account_id(s)
    ddb, sqs, iam, lam = s.client("dynamodb"), s.client("sqs"), s.client("iam"), s.client("lambda")
    run_failed = True
    try:
        made = make_topic_and_queues(s, prefix)
        mappings = {}
        for c in CONSUMERS:
            table_arn = infra.create_table(ddb, n[c], "update_id")
            role = infra.create_role(iam, f"{n[c]}-role", [
                {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                 "Resource": made[c]["arn"]},
                {"Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": table_arn},
            ], region, acct, n[c])
            infra.create_log_group(s.client("logs"), n[c])
            infra.create_function(lam, n[c], role, infra.zip_files({"fanout_consumer.py": HERE / "fanout_consumer.py"}),
                                  "fanout_consumer.handler", {"TABLE": n[c]})
            mappings[c] = infra.create_mapping(
                lam, n[c], made[c]["arn"], BatchSize=10,
                FunctionResponseTypes=["ReportBatchItemFailures"],
                ScalingConfig={"MaximumConcurrency": 2},        # at most 2 copies of the function per queue
                Enabled=(c == "dashboard"))                     # history starts stopped
        infra.wait_mapping(lam, mappings["dashboard"])
        infra.wait_mapping(lam, mappings["history"], state="Disabled")
        t0 = time.time()
        sent = publish(s.client("sns"), made["topic_arn"], make_updates(n_updates), rate=rate)
        wait_count(ddb, n["dashboard"], sent, wait_s)
        while_stopped = {"dashboard stored": ddb.scan(TableName=n["dashboard"], Select="COUNT")["Count"],
                         "history stored": ddb.scan(TableName=n["history"], Select="COUNT")["Count"],
                         "history queue holds": queue_depth(sqs, made["history"]["url"])}
        time.sleep(max(0.0, stopped_for - (time.time() - t0)))
        lam.update_event_source_mapping(UUID=mappings["history"], Enabled=True)
        wait_count(ddb, n["history"], sent, wait_s)
        out = {"sent": sent, "while_stopped": while_stopped,
               "summary": {c: lag.summarize(lag.scan_all(ddb, n[c])) for c in CONSUMERS}}
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
    ap.add_argument("--updates", type=int, default=200)
    ap.add_argument("--stopped-for", type=float, default=None,
                    help="seconds the history consumer stays stopped (default 3 local, 60 aws)")
    ap.add_argument("--rate", type=float, default=10, help="aws only: updates a second (default 10)")
    ap.add_argument("--wait", type=int, default=180)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    if a.target == "local":
        out = run_local(a.updates, 3 if a.stopped_for is None else a.stopped_for, prefix)
    else:
        out = run_aws(a.updates, 60 if a.stopped_for is None else a.stopped_for, prefix, a.rate, a.wait, a.keep)
    print(f"target: {a.target}, published: {out['sent']}")
    print("while history was stopped: " + ", ".join(f"{k} {v}" for k, v in out["while_stopped"].items()))
    for c in CONSUMERS:
        print(f"\n{c} consumer")
        print(lag.report(out["summary"][c]))
    return out


if __name__ == "__main__":
    main()
