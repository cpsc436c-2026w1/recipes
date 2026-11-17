"""Create the DynamoDB tables a real run needs. clean_up.py deletes them again.

Every table is on-demand (PAY_PER_REQUEST): a demo sends a few requests an hour, and on-demand bills per request
with nothing to pay while it idles (README, "Cost and clean-up").
"""

from __future__ import annotations

import boto3


def ensure_table(name: str, region: str, key: str, sort_key: str | None = None, ttl_attribute: str = "expires_at",
                 client=None) -> None:
    ddb = client or boto3.client("dynamodb", region_name=region)
    try:
        status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
    except ddb.exceptions.ResourceNotFoundException:
        status = None
    if status == "DELETING":
        # clean_up.py ran a moment ago: DeleteTable returns before the table is gone. A table in the DELETING
        # state may still take a write, and the item then goes with the table (the trace would be lost). Wait until
        # it is gone, then create it again.
        # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html (read 2026-11-13)
        ddb.get_waiter("table_not_exists").wait(TableName=name)
        status = None
    if status is not None:
        ddb.get_waiter("table_exists").wait(TableName=name)  # returns at once unless it is still CREATING
        return
    schema = [{"AttributeName": key, "KeyType": "HASH"}]
    attrs = [{"AttributeName": key, "AttributeType": "S"}]
    if sort_key:
        schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})
        attrs.append({"AttributeName": sort_key, "AttributeType": "S"})
    ddb.create_table(TableName=name, KeySchema=schema, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST",
                     Tags=[{"Key": "recipe", "Value": "dashboard-chat"}])
    ddb.get_waiter("table_exists").wait(TableName=name)
    # DynamoDB deletes an item within a few days after expires_at passes, not at that second, so a read can still
    # return it; the answer cache checks expires_at itself (ext-answer-cache/cache.py).
    # https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html (read 2026-11-13)
    ddb.update_time_to_live(TableName=name,
                            TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl_attribute})
