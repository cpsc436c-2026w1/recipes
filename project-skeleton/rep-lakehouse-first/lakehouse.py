"""Lakehouse first: updates -> Kinesis Data Streams -> Firehose -> S3 (Parquet) -> Athena -> the number.

Read with the README of the recipe, section Decisions; each "Decision:" comment below names its row.
"""
import json
import shutil
import time
from datetime import datetime
from pathlib import Path


from skeleton.aws import athena_query, ensure_bucket, ensure_role, ensure_workgroup, wait_until_gone
from skeleton.config import REGION, TAG
from skeleton.events import COLUMNS
from skeleton.lake_local import query, write_parquet_batch
from skeleton.number import lake_sql

PUT_RECORDS_MAX = 500          # PutRecords takes at most 500 records per call (Kinesis API reference)
BUFFER_SECONDS = 60            # Decision "buffer": Firehose writes a file every 60 s or 64 MB, whichever comes first
BUFFER_MB = 64                 # the smallest size Firehose allows when it converts to Parquet (Firehose docs)


# ---------------------------------------------------------------- AWS ------------------------------------------------

def glue_table_input(n: dict) -> dict:
    """The schema Firehose converts to and Athena reads. Decision "partitions": dt with partition projection."""
    return {
        "Name": "updates",
        "TableType": "EXTERNAL_TABLE",
        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
        "Parameters": {
            "classification": "parquet",
            "projection.enabled": "true",                    # Athena computes the partitions; no crawler, no MSCK
            "projection.dt.type": "date",
            "projection.dt.format": "yyyy-MM-dd",
            "projection.dt.range": "2026-01-01,NOW",
            "storage.location.template": f"s3://{n['bucket']}/updates/dt=${{dt}}/",
        },
        "StorageDescriptor": {
            "Columns": [{"Name": c, "Type": t, "Comment": d} for c, t, d in COLUMNS],
            "Location": f"s3://{n['bucket']}/updates/",
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"},
        },
    }


def firehose_policy(n: dict, account: str, stream_arn: str) -> dict:
    """Decision "permissions": Firehose may read this one stream, write this one bucket, read this one table."""
    bucket = f"arn:aws:s3:::{n['bucket']}"
    glue = f"arn:aws:glue:{REGION}:{account}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Resource": stream_arn,
         "Action": ["kinesis:DescribeStream", "kinesis:DescribeStreamSummary", "kinesis:GetShardIterator",
                    "kinesis:GetRecords", "kinesis:ListShards"]},
        {"Effect": "Allow", "Resource": [bucket, f"{bucket}/*"],
         "Action": ["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket",
                    "s3:ListBucketMultipartUploads", "s3:PutObject"]},
        {"Effect": "Allow",
         "Resource": [f"{glue}:catalog", f"{glue}:database/{n['database']}", f"{glue}:table/{n['database']}/updates"],
         "Action": ["glue:GetTable", "glue:GetTableVersion", "glue:GetTableVersions"]},
    ]}


def setup(sess, n: dict, account: str, sleep=time.sleep) -> dict:
    s3, glue, kinesis = sess.client("s3"), sess.client("glue"), sess.client("kinesis")
    ensure_bucket(s3, n["bucket"])
    try:
        glue.create_database(DatabaseInput={"Name": n["database"]}, Tags={TAG["Key"]: TAG["Value"]})
    except glue.exceptions.AlreadyExistsException:
        pass
    try:
        glue.create_table(DatabaseName=n["database"], TableInput=glue_table_input(n))
    except glue.exceptions.AlreadyExistsException:
        glue.update_table(DatabaseName=n["database"], TableInput=glue_table_input(n))

    # Decision "stream capacity": one provisioned shard takes up to 1,000 records or 1 MB a second of writes.
    create_stream(kinesis, n["stream"], sleep)
    kinesis.get_waiter("stream_exists").wait(StreamName=n["stream"])
    stream_arn = kinesis.describe_stream_summary(StreamName=n["stream"])["StreamDescriptionSummary"]["StreamARN"]
    kinesis.add_tags_to_stream(StreamName=n["stream"], Tags={TAG["Key"]: TAG["Value"]})

    role = ensure_role(sess.client("iam"), n["firehose_role"], "firehose.amazonaws.com",
                       firehose_policy(n, account, stream_arn), "FIREHOSE_ROLE_ARN")
    create_firehose(sess.client("firehose"), n, stream_arn, role, sleep)
    ensure_workgroup(sess.client("athena"), n["workgroup"], n["bucket"])
    return {"stream_arn": stream_arn, "firehose_role": role}


def create_stream(kinesis, name: str, sleep=time.sleep) -> None:
    """Create the stream, or use the one that is there. One that is still being deleted is waited out first."""
    for _ in range(2):
        try:
            kinesis.create_stream(StreamName=name, ShardCount=1, StreamModeDetails={"StreamMode": "PROVISIONED"})
            return
        except kinesis.exceptions.ResourceInUseException:
            try:
                status = kinesis.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamStatus"]
            except kinesis.exceptions.ResourceNotFoundException:
                continue                    # the deletion finished between the two calls: create it now
            if status != "DELETING":
                return                      # it exists: use it
            wait_until_gone(lambda: kinesis.describe_stream_summary(StreamName=name),
                            kinesis.exceptions.ResourceNotFoundException, sleep)


def firehose_status(firehose, name: str) -> str | None:
    """The stream's status, or None when there is no such stream."""
    try:
        return firehose.describe_delivery_stream(DeliveryStreamName=name)[
            "DeliveryStreamDescription"]["DeliveryStreamStatus"]
    except firehose.exceptions.ResourceNotFoundException:
        return None


def create_firehose(firehose, n: dict, stream_arn: str, role: str, sleep=time.sleep, wait_tries: int = 120) -> None:
    config = {
        "RoleARN": role,
        "BucketARN": f"arn:aws:s3:::{n['bucket']}",
        # Decision "partitions": one folder per arrival day, the layout the Glue table projects
        "Prefix": "updates/dt=!{timestamp:yyyy-MM-dd}/",
        "ErrorOutputPrefix": "errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
        "BufferingHints": {"SizeInMBs": BUFFER_MB, "IntervalInSeconds": BUFFER_SECONDS},
        # Decision "file format": JSON in, Parquet out, schema from the Glue table
        "DataFormatConversionConfiguration": {
            "Enabled": True,
            "SchemaConfiguration": {"RoleARN": role, "DatabaseName": n["database"], "TableName": "updates",
                                    "Region": REGION, "VersionId": "LATEST"},
            "InputFormatConfiguration": {"Deserializer": {"OpenXJsonSerDe": {}}},   # reads 2026-11-12T15:04:05.123Z
            "OutputFormatConfiguration": {"Serializer": {"ParquetSerDe": {"Compression": "SNAPPY"}}},
        },
    }
    if firehose_status(firehose, n["firehose"]) == "DELETING":     # a clean-up a moment ago: wait, then create anew
        wait_until_gone(lambda: firehose.describe_delivery_stream(DeliveryStreamName=n["firehose"]),
                        firehose.exceptions.ResourceNotFoundException, sleep)
    for attempt in range(6):        # a role created seconds ago may not be assumable yet: retry
        try:
            firehose.create_delivery_stream(
                DeliveryStreamName=n["firehose"], DeliveryStreamType="KinesisStreamAsSource",
                KinesisStreamSourceConfiguration={"KinesisStreamARN": stream_arn, "RoleARN": role},
                ExtendedS3DestinationConfiguration=config, Tags=[TAG])
            break
        except firehose.exceptions.ResourceInUseException:
            break
        except firehose.exceptions.InvalidArgumentException:
            if attempt == 5:
                raise
            sleep(10)
    for _ in range(wait_tries):     # ours: up to 10 minutes; a stream that failed to start never becomes ACTIVE
        status = firehose_status(firehose, n["firehose"])
        if status == "ACTIVE":
            return
        if status is None or status.endswith("FAILED"):
            raise RuntimeError(f"Firehose stream {n['firehose']} did not start: {status or 'not found'}")
        sleep(5)
    raise TimeoutError(f"Firehose stream {n['firehose']} is still {status} after {wait_tries * 5} s")


def send(kinesis, stream: str, events: list[dict], rate: float | None = None, sleep=time.sleep) -> int:
    """Write the updates to the stream. Decision "partition key": the entity id, so one key's updates land in one shard."""
    sent = 0
    step = PUT_RECORDS_MAX if rate is None else max(1, int(rate))
    for i in range(0, len(events), step):
        records = [{"Data": (json.dumps(e) + "\n").encode(), "PartitionKey": e["entity_id"]}
                   for e in events[i:i + step]]
        for attempt in range(5):
            r = kinesis.put_records(StreamName=stream, Records=records)
            if r.get("FailedRecordCount", 0) == 0:
                break
            # Decision "retries": resend only the records that failed (throttled), with a growing pause
            records = [rec for rec, res in zip(records, r["Records"]) if "ErrorCode" in res]
            sleep(0.2 * 2 ** attempt)
        else:
            raise RuntimeError(f"{len(records)} records still failing after 5 tries")
        sent += len(events[i:i + step])
        if rate is not None:
            sleep(1)            # about `rate` updates a second, to watch them arrive
    return sent


def wait_for_lake(s3, bucket: str, timeout_s: int = 600, sleep=time.sleep) -> list[str]:
    """Firehose writes after its buffer interval; wait until Parquet files appear and stop changing."""
    deadline, seen = time.time() + timeout_s, -1
    while time.time() < deadline:
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="updates/").get("Contents", [])]
        if keys and len(keys) == seen:
            return keys
        seen = len(keys)
        sleep(BUFFER_SECONDS / 2)
    errors = s3.list_objects_v2(Bucket=bucket, Prefix="errors/").get("KeyCount", 0)
    raise TimeoutError(f"no stable files under updates/ after {timeout_s} s; {errors} objects under errors/")


def number_aws(sess, n: dict, start: datetime, end: datetime) -> tuple[int, int]:
    rows, scanned = athena_query(sess.client("athena"), lake_sql(start, end), n["workgroup"], n["database"])
    return int(rows[0]["updates_last_hour"]), scanned


# ---------------------------------------------------------------- offline --------------------------------------------

def run_local(events: list[dict], root: Path, start: datetime, end: datetime, arrived: datetime,
              buffer_records: int = 200) -> int:
    """The same path without AWS: the list plays the stream, each buffer becomes one Parquet file, DuckDB plays Athena.

    `buffer_records` stands in for Firehose's size and time limits. Each run starts from an empty local lake.
    """
    shutil.rmtree(root / "updates", ignore_errors=True)
    for b, i in enumerate(range(0, len(events), buffer_records)):
        write_parquet_batch(events[i:i + buffer_records], root, arrived, b)
    return int(query(root, lake_sql(start, end))[0]["updates_last_hour"])
