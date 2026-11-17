"""Producer: send updates to a Kinesis data stream with PutRecords.

Decisions (README "Decisions"):
- partition key = driver_id, so one driver's updates go to one shard (PutRecords does not guarantee the order sent);
- batches of up to 500 records (the PutRecords limit), and only the records that failed are retried, after a random
  wait under a limit that doubles, and at most MAX_ATTEMPTS times. A retried record can arrive twice; the consumer
  drops the repeat by update_id.
"""

import json
import random
import time

from updates import stamp

BATCH = 500          # PutRecords takes at most 500 records per call (Kinesis API reference)
MAX_ATTEMPTS = 5     # retry limit for records the stream refused (REL05-BP03)


def put_batch(kinesis, stream: str, records: list[dict]) -> int:
    """Send one batch; retry only the failed entries. Returns how many retries were needed."""
    pending = [{"Data": json.dumps(r).encode(), "PartitionKey": r["driver_id"]} for r in records]
    retries = 0
    for attempt in range(MAX_ATTEMPTS):
        resp = kinesis.put_records(StreamName=stream, Records=pending)
        if not resp.get("FailedRecordCount"):
            return retries
        pending = [e for e, res in zip(pending, resp["Records"]) if res.get("ErrorCode")]
        if attempt + 1 == MAX_ATTEMPTS:
            break                                      # the last attempt: nothing is sent again, so no wait
        retries += len(pending)
        # Backoff with jitter (REL05-BP03): the limit doubles (0.1, 0.2, 0.4, 0.8 s), and the wait is a random part
        # of it, so producers that were refused together do not all come back at the same moment.
        time.sleep(random.uniform(0, 0.1 * 2 ** attempt))
    raise RuntimeError(f"{len(pending)} records still refused after {MAX_ATTEMPTS} attempts")


def send(kinesis, stream: str, updates: list[dict], rate: float = 0.0, clock=time.time) -> dict:
    """Send updates at `rate` updates a second (0 = as fast as possible). Each is stamped when it is sent."""
    sent = retries = 0
    start = clock()
    step = 10 if rate else BATCH                        # small batches when pacing, so send times stay close
    for i in range(0, len(updates), step):
        if rate:
            due = start + i / rate
            pause = due - clock()
            if pause > 0:
                time.sleep(pause)
        now_ms = int(clock() * 1000)
        batch = [stamp(u, now_ms) for u in updates[i:i + step]]
        retries += put_batch(kinesis, stream, batch)
        sent += len(batch)
    return {"sent": sent, "retried": retries, "seconds": round(clock() - start, 1)}
