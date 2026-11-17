"""Create the recipe's DynamoDB tables, on demand, each with time to live on `expires_at`.

| Table                         | Partition key    | Sort key       | Used by            |
|-------------------------------|------------------|----------------|--------------------|
| {PREFIX}-chat-conversations   | conversation_id  | -              | main/history.py    |
| {PREFIX}-chat-history         | conversation_id  | at (time, text)| main/history.py    |
| {PREFIX}-answer-cache         | cache_key        | -              | ext-answer-cache/  |
| {PREFIX}-llm-traces           | request_id       | seq (number)   | ext-traces/        |
"""

from __future__ import annotations

from config import Settings

KEYS = {
    "conversations": ([("conversation_id", "S", "HASH")]),
    "history": ([("conversation_id", "S", "HASH"), ("at", "S", "RANGE")]),
    "cache": ([("cache_key", "S", "HASH")]),
    "traces": ([("request_id", "S", "HASH"), ("seq", "N", "RANGE")]),
}


def table_name(s: Settings, kind: str) -> str:
    return {"conversations": s.conversations_table, "history": s.history_table,
            "cache": s.cache_table, "traces": s.traces_table}[kind]


def ensure_tables(s: Settings, kinds: list[str], ddb) -> list[str]:
    """Create the tables that are missing; return the names of those created. `ddb` is a boto3 DynamoDB client.

    A table that clean_up.py is still deleting is waited for and created again."""
    created = []
    for kind in kinds:
        name = table_name(s, kind)
        try:
            status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
        except ddb.exceptions.ResourceNotFoundException:
            status = None
        if status == "DELETING":
            # clean_up.py ran a moment ago: DeleteTable returns before the table is gone, its items go with it,
            # and a table of that name cannot be created until it is gone. Wait, then create it again.
            ddb.get_waiter("table_not_exists").wait(TableName=name)
            status = None
        if status is not None:
            ddb.get_waiter("table_exists").wait(TableName=name)  # returns at once unless it is still CREATING
            continue
        keys = KEYS[kind]
        # On demand: a sandbox with a few requests a day pays per request and nothing while idle
        # (README, "Decisions": capacity mode).
        ddb.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": t} for n, t, _ in keys],
            KeySchema=[{"AttributeName": n, "KeyType": k} for n, _, k in keys],
            Tags=[{"Key": "recipe", "Value": "llm-state-dynamodb"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=name)
        # TTL lets DynamoDB delete old items without write cost, some time after expiry (often days later),
        # so every reader still filters on expires_at itself (README, "Decisions": expiry by TTL).
        ddb.update_time_to_live(TableName=name,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
        created.append(name)
    return created
