"""Move the index to a new mapping version, and take a principal out of the data access policy.

  INDEX_VERSION=2 uv run python -m main.run --target aws --setup   1. create items-v2, point the function at it
  INDEX_VERSION=2 uv run python -m main.migrate backfill        2. send every item of the table through the stream
  INDEX_VERSION=2 uv run python -m main.migrate check           2b. does items-v2 hold every item of the table?
  uv run python -m main.migrate drop items-v1                   3. once the app reads items-v2: remove items-v1
  uv run python -m main.migrate drop-principal <role arn>       a teammate's role, when they leave the team

Step 1 also adds items-v2 to the data access policy beside items-v1, so your app keeps reading items-v1 while
items-v2 fills; from then on the function writes only to items-v2, so items-v1 stops following the table.

Step 2 goes through the table, not around it: it sets one attribute, `index_backfill`, on every item (only if the
item still exists), so each item's change reaches the function through the stream, in order with every other
change to that item. A copy sent straight to the index could land after a newer change and overwrite it; this
cannot. It costs one write per item, and the items carry that extra attribute (unmapped, so not searchable).

Step 3 refuses to drop the index the deployed function writes to (read from the function's settings, not from your
INDEX_VERSION). drop-principal refuses to remove you or the function's role.
"""

from __future__ import annotations

import argparse
import json
import time

import boto3

from main.config import Settings
from main.infra import caller_principal, merge_data_policy


def backfill(table, marker: str, sleep=time.sleep) -> dict:
    """Touch every item so its current state goes through the stream. Returns counts."""
    counts = {"items": 0, "gone meanwhile": 0}
    kw: dict = {"ProjectionExpression": "id"}
    while True:
        page = table.scan(**kw)
        for it in page["Items"]:
            try:
                table.update_item(Key={"id": it["id"]}, UpdateExpression="SET index_backfill = :m",
                                  ConditionExpression="attribute_exists(id)",   # never bring back a deleted item
                                  ExpressionAttributeValues={":m": f"{marker} {time.time():.3f}"})
                counts["items"] += 1
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                counts["gone meanwhile"] += 1
        if "LastEvaluatedKey" not in page:
            return counts
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def check(s: Settings, client=None, table=None, sqs=None) -> tuple[str, int]:
    """Does the index hold every item of the table? (line, exit code). Run it before you move the app's reads to a
    new index (README, Adapt it, step 3), or any time the index looks out of step. The failure queue's messages are
    counted, not read, so checking takes nothing out of the queue. Data Analytics Lens, best practice 7.2 (monitor
    for data quality anomalies): a count that does not match is the anomaly a derived index can have."""
    if client is None:
        from main.envs import AwsEnv

        client = AwsEnv(s).client
    table = table or boto3.resource("dynamodb", region_name=s.region).Table(s.table)
    sqs = sqs or boto3.client("sqs", region_name=s.region)
    items, kw = 0, {"Select": "COUNT"}
    while True:
        page = table.scan(**kw)
        items += page["Count"]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    docs = _count_when_let_in(client, s.index)
    url = sqs.get_queue_url(QueueName=s.failure_queue)["QueueUrl"]
    queued = int(sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"]["ApproximateNumberOfMessages"])
    line = f"table {s.table}: {items} items; index {s.index}: {docs} documents; failure queue: {queued} message(s)"
    if docs == items:
        return line + "; the index holds every item", 0
    return line + ("; the difference is in the failure queue or still on its way: read the queue, fix the items in "
                   "the table" if queued else "; still on its way, or lost: run check again in a minute"), 1


def _count_when_let_in(client, index: str, wait_s: float = 300, every_s: float = 10) -> int:
    """The index's document count. Right after a set-up the changed data access policy may not apply yet (401 or
    403); try again every 10 s for up to 300 s, as the set-up does (R38)."""
    from opensearchpy.exceptions import AuthenticationException, AuthorizationException

    deadline = time.time() + wait_s
    while True:
        try:
            return client.count(index=index)["count"]
        except (AuthenticationException, AuthorizationException) as err:
            if time.time() + every_s > deadline:
                raise
            print(f"refused ({err.status_code}); trying again in {every_s:.0f} s", flush=True)
            time.sleep(every_s)


def deployed(s: Settings, lam=None) -> dict:
    """The function's current index and role, as AWS has them."""
    lam = lam or boto3.client("lambda", region_name=s.region)
    conf = lam.get_function_configuration(FunctionName=s.function)
    return {"index": conf.get("Environment", {}).get("Variables", {}).get("INDEX", ""), "role": conf["Role"]}


def drop(s: Settings, old_index: str, client=None, aoss=None, lam=None) -> str:
    live = deployed(s, lam)["index"]
    if old_index == live:
        raise SystemExit(f"{old_index} is the index the function {s.function} writes to now; move it first")
    aoss = aoss or boto3.client("opensearchserverless", region_name=s.region)
    if client is None:
        from main.envs import AwsEnv

        client = AwsEnv(s).client
    if client.indices.exists(index=old_index):
        client.indices.delete(index=old_index)
    # Read, merge, update: never a create here. The service checks a new policy's shape before it says the name is
    # taken, so creating an empty policy to merge into is refused (measured 2026-11-13).
    current = aoss.get_access_policy(name=s.collection, type="data")["accessPolicyDetail"]
    new = merge_data_policy(current["policy"], [], drop=(f"index/{s.collection}/{old_index}",))
    if new != current["policy"]:
        aoss.update_access_policy(name=s.collection, type="data", policy=json.dumps(new),
                                  policyVersion=current["policyVersion"])
    return f"{old_index} deleted and taken out of the data access policy of {s.collection}"


def drop_principal(s: Settings, arn: str, aoss=None, lam=None, me: str | None = None) -> str:
    """A rerun only adds principals to the data access policy (main/infra.py merge_data_policy); this takes one out."""
    me = me or caller_principal(s.region)
    if arn in (me, deployed(s, lam)["role"]):
        raise SystemExit(f"{arn} is you or the function's role: removing it would lock you or the function out")
    aoss = aoss or boto3.client("opensearchserverless", region_name=s.region)
    current = aoss.get_access_policy(name=s.collection, type="data")["accessPolicyDetail"]
    new = merge_data_policy(current["policy"], [], drop_principals=(arn,))
    if new == current["policy"]:
        return f"{arn} is not in the data access policy of {s.collection}"
    aoss.update_access_policy(name=s.collection, type="data", policy=json.dumps(new),
                              policyVersion=current["policyVersion"])
    return f"{arn} taken out of the data access policy of {s.collection}"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="step", required=True)
    sub.add_parser("backfill")
    sub.add_parser("check")
    d = sub.add_parser("drop")
    d.add_argument("old_index")
    dp = sub.add_parser("drop-principal")
    dp.add_argument("arn")
    args = p.parse_args(argv)
    s = Settings()
    if args.step == "backfill":
        table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
        print(backfill(table, s.index))
    elif args.step == "check":
        line, code = check(s)
        print(line)
        if code:
            raise SystemExit(code)
    elif args.step == "drop":
        print(drop(s, args.old_index))
    else:
        print(drop_principal(s, args.arn))


if __name__ == "__main__":
    main()
