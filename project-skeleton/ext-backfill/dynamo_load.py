"""The history into DynamoDB: an import from S3 for a new table, BatchWriteItem for one that exists.

Both stamp every item with the load's own `load_id` beside the history row's `run_id`, and both end with an exact
count against the rows read: `DescribeImport` (`ImportedItemCount`, `ErrorCount`) after an import, a strongly
consistent `Scan` with `Select=COUNT` of this `load_id` after batch writes.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import random
import time
from datetime import date, datetime
from decimal import Decimal

from tags import record, recorded, tags

BATCH = 25                      # BatchWriteItem takes at most 25 puts in one call
ATTEMPTS = 5                    # a batch is sent at most 5 times
FIRST_LIMIT_S = 0.1             # the first wait is random under 0.1 s; each further limit doubles
LINES_PER_FILE = 100_000
IMPORT_POLL_S = 10
IMPORT_TIMEOUT_S = 1800
DONE = ("COMPLETED", "FAILED", "CANCELLED")
KEY = "trip_id"


class Unprocessed(RuntimeError):
    pass


def number(v) -> str:
    """A number as DynamoDB takes it: plain digits, no exponent (18.4 -> "18.4")."""
    if isinstance(v, bool):
        raise TypeError("a bool is not a number")
    if isinstance(v, int):
        return str(v)
    return format(Decimal(repr(float(v))).normalize(), "f")


def text(v) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def to_item(row: dict, load_id: str) -> dict:
    """A history row as a DynamoDB item: numbers as N, text and times as S, a null column left out."""
    item = {}
    for k, v in {**row, "load_id": load_id}.items():
        if v is None:
            continue
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            item[k] = {"N": number(v)}
        else:
            item[k] = {"S": text(v)}
    return item


def import_files(items: list[dict], fmt: str) -> list[bytes]:
    """The import's files, gzip: DynamoDB JSON (one {"Item": ...} a line) or CSV with a header line."""
    out = []
    for i in range(0, max(len(items), 1), LINES_PER_FILE):
        part = items[i:i + LINES_PER_FILE]
        if fmt == "dynamodb_json":
            body = "".join(json.dumps({"Item": it}, sort_keys=True) + "\n" for it in part)
        else:
            columns = sorted({k for it in items for k in it}, key=lambda c: (c != KEY, c))
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(columns)
            for it in part:
                w.writerow([next(iter(it[c].values())) if c in it else "" for c in columns])
            body = buf.getvalue()
        out.append(gzip.compress(body.encode()))
    return out


def table_exists(ddb, table: str) -> bool:
    try:
        ddb.describe_table(TableName=table)
        return True
    except ddb.exceptions.ResourceNotFoundException:
        return False


def import_new(sess, table: str, bucket: str, prefix: str, items: list[dict], fmt: str, log=print,
               sleep=time.sleep) -> int:
    """Write the files, ImportTable, wait for the end, compare the counts. Returns the exit code."""
    s3, ddb = sess.client("s3"), sess.client("dynamodb")
    for n, body in enumerate(import_files(items, fmt)):
        ext = "json" if fmt == "dynamodb_json" else "csv"
        s3.put_object(Bucket=bucket, Key=f"{prefix}part-{n:05d}.{ext}.gz", Body=body)
    log(f"wrote    {len(items)} lines as {fmt.upper()} under s3://{bucket}/{prefix}")
    desc = ddb.import_table(
        S3BucketSource={"S3Bucket": bucket, "S3KeyPrefix": prefix},
        InputFormat="DYNAMODB_JSON" if fmt == "dynamodb_json" else "CSV",
        InputCompressionType="GZIP",
        TableCreationParameters={"TableName": table, "BillingMode": "PAY_PER_REQUEST",
                                 "AttributeDefinitions": [{"AttributeName": KEY, "AttributeType": "S"}],
                                 "KeySchema": [{"AttributeName": KEY, "KeyType": "HASH"}]})["ImportTableDescription"]
    arn = desc["ImportArn"]
    log(f"import   {arn.rsplit('/', 1)[-1]} started: table {table}")
    deadline = time.time() + IMPORT_TIMEOUT_S
    while desc.get("ImportStatus") not in DONE:
        if time.time() > deadline:
            log(f"FAILED   import still {desc.get('ImportStatus')} after {IMPORT_TIMEOUT_S} s; "
                f"aws dynamodb describe-import --import-arn {arn}")
            return 1
        sleep(IMPORT_POLL_S)
        desc = ddb.describe_import(ImportArn=arn)["ImportTableDescription"]
    status = desc["ImportStatus"]
    imported, errors = desc.get("ImportedItemCount", 0), desc.get("ErrorCount", 0)
    if status == "COMPLETED":
        table_arn = desc.get("TableArn") or ddb.describe_table(TableName=table)["Table"]["TableArn"]
        # ImportTable takes no tags (its TableCreationParameters has none): the table is tagged once it exists
        ddb.tag_resource(ResourceArn=table_arn, Tags=tags("dynamodb"))
        record("dynamodb-table", table, "created", arn=table_arn, script="ext-backfill/backfill.py --to-dynamodb",
               clean="ext-backfill/clean_up.py --yes", bills="monthly")
        keep_import_logs(sess, desc.get("CloudWatchLogGroupArn"), log)
    log(f"{imported} items imported, {len(items)} rows read, {errors} errors"
        + ("" if status == "COMPLETED" else f" (import {status}: {desc.get('FailureCode', '')} "
                                            f"{desc.get('FailureMessage', '')})"))
    return 0 if status == "COMPLETED" and imported == len(items) and errors == 0 else 1


def keep_import_logs(sess, group_arn: str | None, log=print) -> None:
    """The import writes a log group only when an item fails; keep its lines 7 days, not for ever."""
    if not group_arn:
        return
    name = group_arn.split(":log-group:")[-1].removesuffix(":*")
    logs = sess.client("logs")
    try:
        logs.put_retention_policy(logGroupName=name, retentionInDays=7)
        if recorded("log-group", name) is None:
            record("log-group", name, "borrowed", script="ext-backfill/backfill.py --to-dynamodb",
                   clean="kept: written by DynamoDB for every import of the account, 7 days")
        log(f"logs     {name}: lines kept 7 days")
    except logs.exceptions.ResourceNotFoundException:
        pass


def create_for_batch(sess, table: str, log=print) -> None:
    """--load batch: the table made by the script (the fallback where ImportTable is refused)."""
    ddb = sess.client("dynamodb")
    arn = ddb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                           AttributeDefinitions=[{"AttributeName": KEY, "AttributeType": "S"}],
                           KeySchema=[{"AttributeName": KEY, "KeyType": "HASH"}],
                           Tags=tags("dynamodb"))["TableDescription"]["TableArn"]
    ddb.get_waiter("table_exists").wait(TableName=table)
    record("dynamodb-table", table, "created", arn=arn, script="ext-backfill/backfill.py --to-dynamodb --load batch",
           clean="ext-backfill/clean_up.py --yes", bills="monthly")
    log(f"created  table {table} (on demand) for batch writes")


def batch_write(ddb, table: str, items: list[dict], sleep=time.sleep, rng: random.Random | None = None,
                log=print) -> int:
    """25 items a call, each key once; UnprocessedItems sent again after a random wait under a limit that doubles
    (0.1, 0.2, 0.4, 0.8 s), at most 5 attempts a batch. Returns the number of calls."""
    rng = rng or random.Random()
    calls = 0
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        keys = [it[KEY]["S"] for it in chunk]
        if len(set(keys)) != len(keys):
            raise ValueError(f"a batch repeats a trip_id: {sorted(k for k in keys if keys.count(k) > 1)[:3]}")
        request = [{"PutRequest": {"Item": it}} for it in chunk]
        for attempt in range(1, ATTEMPTS + 1):
            calls += 1
            left = ddb.batch_write_item(RequestItems={table: request}).get("UnprocessedItems", {}).get(table, [])
            if not left:
                break
            if attempt == ATTEMPTS:
                first = left[0]["PutRequest"]["Item"][KEY]["S"]
                raise Unprocessed(f"{len(left)} items unprocessed after {ATTEMPTS} attempts; the first is trip_id "
                                  f"{first}")
            sleep(rng.uniform(0, FIRST_LIMIT_S * 2 ** (attempt - 1)))
            request = left
    return calls


def count_load(ddb, table: str, load_id: str) -> int:
    """The items of this load, counted exactly: Select=COUNT, ConsistentRead, a filter on load_id, every page."""
    n, start = 0, None
    while True:
        page = ddb.scan(TableName=table, Select="COUNT", ConsistentRead=True,
                        FilterExpression="#l = :l", ExpressionAttributeNames={"#l": "load_id"},
                        ExpressionAttributeValues={":l": {"S": load_id}},
                        **({"ExclusiveStartKey": start} if start else {}))
        n += page["Count"]
        start = page.get("LastEvaluatedKey")
        if not start:
            return n


def load(sess, table: str, bucket: str | None, rows: list[dict], load_id: str, fmt: str = "dynamodb_json",
         mode: str = "auto", log=print, sleep=time.sleep) -> int:
    """Load the rows into the table; returns the exit code."""
    ddb = sess.client("dynamodb")
    items = [to_item(r, load_id) for r in rows]
    exists = table_exists(ddb, table)
    if not exists and mode != "batch":
        return import_new(sess, table, bucket, f"imports/trips-by-id/{load_id}/", items, fmt, log, sleep)
    if not exists:
        create_for_batch(sess, table, log)
    elif recorded("dynamodb-table", table) is None:
        record("dynamodb-table", table, "borrowed", script="ext-backfill/backfill.py --to-dynamodb")
    try:
        calls = batch_write(ddb, table, items, sleep=sleep, log=log)
    except Unprocessed as e:
        log(f"FAILED   {e}")
        raise SystemExit(1) from e
    log(f"wrote    {len(items)} items to {table} in {calls} BatchWriteItem calls")
    n = count_load(ddb, table, load_id)
    log(f"{n} items of load {load_id}, {len(rows)} rows read")
    return 0 if n == len(rows) else 1
