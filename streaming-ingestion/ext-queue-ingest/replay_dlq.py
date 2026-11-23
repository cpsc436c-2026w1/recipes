"""Move every message of the dead-letter queue back to its source queue, once the fault is fixed.

    uv run python ext-queue-ingest/replay_dlq.py            # a message move task; the copy path if it is refused
    uv run python ext-queue-ingest/replay_dlq.py --copy     # receive, send, then delete each message yourself
    uv run python ext-queue-ingest/replay_dlq.py --probe    # is StartMessageMoveTask allowed? one test message

First it starts one message move task (StartMessageMoveTask) from `$PREFIX-trip-completed-dlq` back to
`$PREFIX-trip-completed` at 10 messages a second, waits until ListMessageMoveTasks reports it finished, and prints
the number moved. If the start is refused with AccessDenied (or with --copy), it moves the messages itself: it
receives up to 10, sends each to the source queue, and deletes it from the dead-letter queue only after the send
went through, at most 10 a second, until a receive returns none. A send that fails leaves its message in the
dead-letter queue. It prints `moved N messages` or `moved N messages by copy[, F failed]`; the exit code is 1 when
a message could not be moved.

Runs with your own sign-in: on the dead-letter queue sqs:StartMessageMoveTask, sqs:ListMessageMoveTasks,
sqs:ReceiveMessage, sqs:DeleteMessage, sqs:GetQueueAttributes, and on the source queue sqs:SendMessage (the copy
path needs all but the two move-task actions).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from botocore.exceptions import ClientError

RATE = 10                 # messages a second, both paths: the consumer is limited to 2 copies (README, Decisions)
DENIED = {"AccessDenied", "AccessDeniedException", "AWS.SimpleQueueService.AccessDenied"}
DONE = {"COMPLETED", "FAILED", "CANCELLED"}


def _url_arn(sqs, name: str) -> tuple[str, str]:
    url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    return url, sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]


def by_task(sqs, dlq_arn: str, source_arn: str, poll_s: float = 5.0, wait_s: float = 900.0, sleep=time.sleep) -> int:
    """One message move task; the number it moved. Raises ClientError when the start is refused."""
    sqs.start_message_move_task(SourceArn=dlq_arn, DestinationArn=source_arn, MaxNumberOfMessagesPerSecond=RATE)
    end = time.time() + wait_s
    while True:
        tasks = sqs.list_message_move_tasks(SourceArn=dlq_arn, MaxResults=1).get("Results", [])
        if tasks and tasks[0]["Status"] in DONE:
            if tasks[0]["Status"] != "COMPLETED":
                raise RuntimeError(f"the move task ended {tasks[0]['Status']}: {tasks[0].get('FailureReason', '')}")
            return int(tasks[0].get("ApproximateNumberOfMessagesMoved", 0))
        if time.time() >= end:
            raise TimeoutError(f"the move task did not finish in {wait_s:.0f} s")
        sleep(poll_s)


def by_copy(sqs, dlq_url: str, source_url: str, sleep=time.sleep, clock=time.monotonic) -> tuple[int, int]:
    """Receive, send, then delete; (moved, failed). A failed send leaves the message where it was."""
    moved = failed = 0
    seen: set[str] = set()
    while True:
        msgs = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                   VisibilityTimeout=60, MessageAttributeNames=["All"]).get("Messages", [])
        for m in msgs:
            if m["MessageId"] in seen:          # one whose send failed earlier in this replay: leave it visible
                sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
        msgs = [m for m in msgs if m["MessageId"] not in seen]
        if not msgs:
            return moved, failed
        for m in msgs:
            start = clock()
            seen.add(m["MessageId"])
            try:
                kw = {"MessageAttributes": m["MessageAttributes"]} if m.get("MessageAttributes") else {}
                sqs.send_message(QueueUrl=source_url, MessageBody=m["Body"], **kw)
            except ClientError as err:
                failed += 1
                print(f"not moved {m['MessageId']}: {err.response['Error']['Code']}")
                # visible again at once: it stays in the dead-letter queue for the next replay
                sqs.change_message_visibility(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
                continue
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
            moved += 1
            pause = 1 / RATE - (clock() - start)
            if pause > 0:
                sleep(pause)


def replay(sqs, dlq: str, source: str, copy: bool = False, sleep=time.sleep) -> int:
    """Move the dead-letter queue back; print what was done; return the exit code."""
    dlq_url, dlq_arn = _url_arn(sqs, dlq)
    source_url, source_arn = _url_arn(sqs, source)
    if not copy:
        try:
            n = by_task(sqs, dlq_arn, source_arn, sleep=sleep)
            print(f"moved {n} messages from {dlq} to {source} with a message move task")
            return 0
        except ClientError as err:
            if err.response["Error"]["Code"] not in DENIED:
                raise
            print(f"StartMessageMoveTask refused ({err.response['Error']['Code']}): moving the messages by copy")
    moved, failed = by_copy(sqs, dlq_url, source_url, sleep=sleep)
    print(f"moved {moved} messages by copy" + (f", {failed} failed" if failed else "") + f" from {dlq} to {source}")
    return 1 if failed else 0


def probe(sqs, dlq: str, source: str, sleep=time.sleep) -> int:
    """Send one test message to the dead-letter queue and move it with a task: whether the lease allows it."""
    dlq_url, dlq_arn = _url_arn(sqs, dlq)
    _, source_arn = _url_arn(sqs, source)
    sqs.send_message(QueueUrl=dlq_url, MessageBody='{"probe": "replay_dlq.py --probe"}')
    try:
        n = by_task(sqs, dlq_arn, source_arn, sleep=sleep)
    except ClientError as err:
        print(f"StartMessageMoveTask: refused ({err.response['Error']['Code']}); replay_dlq.py will use the copy "
              f"path. The test message is still in {dlq}: run replay_dlq.py --copy, and the consumer counts it failed")
        return 1
    print(f"StartMessageMoveTask: allowed; {n} message moved to {source} (the consumer reports it failed, and it "
          f"comes back to {dlq} after 5 receives: delete it there with the console or purge-queue)")
    return 0


def main(argv=None) -> int:
    import boto3

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--copy", action="store_true", help="skip the move task")
    p.add_argument("--probe", action="store_true", help="check that StartMessageMoveTask is allowed")
    args = p.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    dlq, source = f"{prefix}-trip-completed-dlq", f"{prefix}-trip-completed"
    return probe(sqs, dlq, source) if args.probe else replay(sqs, dlq, source, copy=args.copy)


if __name__ == "__main__":
    sys.exit(main())
