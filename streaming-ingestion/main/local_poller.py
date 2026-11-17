"""Local stand-in for Lambda's event source mapping, for `--target local` and the tests.

In AWS, Lambda polls each shard and calls the function with a batch. Here we read the shards with GetRecords and call
the same handler with an event of the same shape (base64 data, approximateArrivalTimestamp in epoch seconds,
sequenceNumber). On a reported failure we retry from that record, as Lambda does, up to a retry limit, and then give
up on the rest of the batch.
"""

import base64


def _event(stream_arn: str, shard: str, records: list[dict]) -> dict:
    return {"Records": [{
        "eventSource": "aws:kinesis",
        "eventSourceARN": stream_arn,
        "eventID": f"{shard}:{r['SequenceNumber']}",
        "kinesis": {
            "partitionKey": r["PartitionKey"],
            "sequenceNumber": r["SequenceNumber"],
            "data": base64.b64encode(r["Data"]).decode(),
            "approximateArrivalTimestamp": r["ApproximateArrivalTimestamp"].timestamp(),
        },
    } for r in records]}


def drain(kinesis, stream: str, handler, batch_size=100, max_retries=3) -> dict:
    """Read every shard from the start and pass batches to `handler(event)`. Returns counts."""
    arn = kinesis.describe_stream_summary(StreamName=stream)["StreamDescriptionSummary"]["StreamARN"]
    calls = gave_up = 0
    for shard in kinesis.list_shards(StreamName=stream)["Shards"]:
        sid = shard["ShardId"]
        it = kinesis.get_shard_iterator(StreamName=stream, ShardId=sid,
                                        ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
        pending = []
        while True:
            if not pending:
                page = kinesis.get_records(ShardIterator=it, Limit=batch_size)
                pending, it = page["Records"], page.get("NextShardIterator")
                if not pending:
                    break
            for attempt in range(max_retries + 1):
                calls += 1
                failures = handler(_event(arn, sid, pending)).get("batchItemFailures", [])
                if not failures:
                    pending = []
                    break
                first = failures[0]["itemIdentifier"]
                pending = [r for r in pending if int(r["SequenceNumber"]) >= int(first)]
            else:
                # Retries used up: give up on the rest of the batch, from the failed record on. Lambda would also split
                # the batch (BisectBatchOnFunctionError) and so store the good records among the rest, and would send
                # the batch's details to the on-failure queue; this stand-in does neither.
                gave_up += 1
                pending = []
    return {"handler_calls": calls, "batches_given_up": gave_up}
