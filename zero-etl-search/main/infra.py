"""Create the real path in your sandbox: table and stream, failure queue, role, collection group, collection, index,
the indexing function and its event source mapping.

Written from the AWS docs (pages linked in the README, Decisions) and the boto3 service models. Every name comes
from PREFIX (main/config.py); clean_up.py removes all of it. Each create below says what the resource is, why it is
set this way, and what it costs or limits; prices are from the AWS bulk price list for ca-central-1, read
2026-11-12.

The function's role is created by ensure_role() with only what the function does (SEC03-BP02), tagged
recipe=zero-etl-search so clean_up.py removes it. To use a role you already have, set LAMBDA_ROLE_ARN; it must
trust lambda.amazonaws.com and be allowed (indexer_statements below):
  dynamodb  DescribeStream, GetRecords, GetShardIterator, ListStreams on table/<name>/stream/*
  sqs       SendMessage on the failure queue
  logs      CreateLogStream, PutLogEvents on its own log group
  aoss      APIAccessAll on the collection (the data access policy then says which index and what it may do)
"""

from __future__ import annotations

import io
import json
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

from main.config import ROOT, Settings
from main.mappings import index_body

TAG = {"Key": "recipe", "Value": "zero-etl-search"}   # clean_up.py deletes only roles that carry this tag
# Index 1 is ext-embeddings' role, index 2 ext-zero-etl's; clean_up.py tries all three.
ROLE_SUFFIXES = ("-search-indexer-role", "-reembed-enqueue-role", "-search-pipeline-role")

# --- the limits, in one place (README, Decisions) ---------------------------------------------------------------
# OpenSearch Serverless collection group, NextGen. Valid values are 0, 2, 4, 8, 16, then multiples of 16 (AWS docs,
# "Collection group capacity limits"), so 2 is the smallest maximum above 0: 1 is refused.
MIN_OCU = 0          # indexing and search each scale to zero after 10 idle minutes: no OCU billed while idle
MAX_OCU = 2          # the most the group may scale to, for indexing and again for search: at most 2 x 0.266 USD
                     # an hour for each (4 OCU, 1.06 USD an hour, while busy)
# Lambda event source mapping on the table's stream (AWS docs, "Lambda parameters for Amazon DynamoDB event source
# mappings").
BATCH_SIZE = 10      # ours: at most 10 changes per call; default 100, maximum 10,000. Small, so a refused record
                     # is split off in a few calls, and one _bulk request stays small
BATCH_WINDOW_S = 0   # the default: call the function as soon as records are there (lowest lag, more calls)
MAX_RETRIES = 10     # ours: Lambda's tries after the first, then the record goes to the failure queue. The default
                     # (-1) retries until the record is 24 hours old, and the shard waits behind it all that time.
                     # The function itself retries temporary errors for up to about 30 s per try (main/handler.py),
                     # so ten tries ride out several minutes of a collection waking up or a policy spreading
MAX_RECORD_AGE_S = 3600  # ours: the cap on all those tries: a record older than an hour goes to the failure queue
FUNCTION_TIMEOUT_S = 60  # room for the function's own retries (MAX_ATTEMPTS and BACKOFF_S in main/handler.py)
FUNCTION_MEMORY_MB = 128  # the smallest; the function only sends one HTTP request per batch
PITR_DAYS = 7        # ours: a week to notice a bad write; the API takes 1 to 35
PITR_WAIT_S = 60     # ours: a minute for a new table's backups to become available
ITERATOR_AGE_ALARM_MS = 300_000  # ours: 5 minutes; see create_alarms()


def ensure_role(iam, name: str, service: str, statements: list[dict], given: str = "", sleep=time.sleep) -> str:
    """A role for one service, allowed only `statements`. If `given` (an existing role's ARN) is set, it is used and
    nothing is created."""
    if given:
        return given
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=[TAG])["Role"]["Arn"]
        fresh = True
    except iam.exceptions.EntityAlreadyExistsException:
        arn, fresh = iam.get_role(RoleName=name)["Role"]["Arn"], False
    iam.put_role_policy(RoleName=name, PolicyName="least-privilege",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))
    if fresh:
        sleep(10)       # a new role takes a few seconds before a service can assume it (assumed: 10 s)
    return arn


def indexer_statements(s: Settings, account: str, stream_arn: str, queue_arn: str,
                       collection_arn: str = "") -> list[dict]:
    """What the indexing function may do: read this table's stream, send to its failure queue, write its own logs,
    and call this one collection's data API. Nothing else (SEC03-BP02)."""
    statements = [
        {"Effect": "Allow", "Resource": stream_arn,
         "Action": ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator",
                    "dynamodb:ListStreams"]},
        # Lambda sends a batch it gave up on with the function's role (AWS docs, on-failure destinations).
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": queue_arn},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{s.region}:{account}:log-group:{s.log_group}:*"},
    ]
    if collection_arn:   # known only once the collection exists; a second ensure_role() call adds it
        statements.append({"Effect": "Allow", "Action": ["aoss:APIAccessAll"], "Resource": collection_arn})
    return statements


def account_id(region: str) -> str:
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def caller_principal(region: str) -> str:
    """The role (or user) behind the current credentials, in the form an access policy wants."""
    arn = boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        return arn
    role = arn.split(":assumed-role/")[1].split("/")[0]
    try:
        return boto3.client("iam").get_role(RoleName=role)["Role"]["Arn"]  # keeps the path SSO roles have
    except Exception:  # noqa: BLE001 - a sandbox may not allow iam:GetRole
        return f"arn:aws:iam::{arn.split(':')[4]}:role/{role}"


def create_table(ddb, s: Settings, items: list[dict] = ()) -> str:
    """The source of truth. Returns the table's stream ARN."""
    # A table that clean_up.py is still deleting answers "in use" to a create, and then disappears: wait until it
    # is gone, so the create below makes a new one (a run right after a clean-up).
    try:
        deleting = ddb.describe_table(TableName=s.table)["Table"]["TableStatus"] == "DELETING"
    except ddb.exceptions.ResourceNotFoundException:
        deleting = False
    if deleting:
        ddb.get_waiter("table_not_exists").wait(TableName=s.table)
    try:
        # DynamoDB table, on demand: billed per request (0.6875 USD per million writes in ca-central-1), nothing
        # while idle; a few kilobytes of storage. The stream keeps every change for 24 hours. NEW_AND_OLD_IMAGES:
        # the function needs the new image; ext-embeddings compares old and new text. Lambda's reads of the stream
        # are not billed (AWS docs, "Process DynamoDB records with Lambda").
        ddb.create_table(
            TableName=s.table, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
            Tags=[{"Key": "recipe", "Value": "zero-etl-search"}])
    except ddb.exceptions.ResourceInUseException:
        pass
    ddb.get_waiter("table_exists").wait(TableName=s.table)
    # Point-in-time recovery: the table is the source of truth, and the index can be rebuilt from it, but nothing
    # rebuilds the table after a bad write or delete. Restore to any second of the last PITR_DAYS days. Billed per
    # GB-month of table size (0.22 USD in ca-central-1, AWS bulk price list read 2026-11-13): this table is a few
    # kilobytes (DynamoDB Well-Architected Lens, reliability pillar: a backup strategy that fits your RPO).
    enable_pitr(ddb, s.table)
    if items:
        write_items(boto3.resource("dynamodb", region_name=s.region).Table(s.table), items)
    return ddb.describe_table(TableName=s.table)["Table"]["LatestStreamArn"]


def enable_pitr(ddb, table: str, wait_s: float = PITR_WAIT_S, every_s: float = 10, sleep=None) -> None:
    """Point-in-time recovery, PITR_DAYS days. A table that has just become ACTIVE can answer
    ContinuousBackupsUnavailableException ("Backups have not yet been enabled for this table", API reference,
    UpdateContinuousBackups): try again every `every_s` seconds for up to `wait_s`."""
    sleep = sleep or time.sleep
    deadline = time.time() + wait_s
    while True:
        try:
            ddb.update_continuous_backups(TableName=table, PointInTimeRecoverySpecification={
                "PointInTimeRecoveryEnabled": True, "RecoveryPeriodInDays": PITR_DAYS})
            return
        except ddb.exceptions.ContinuousBackupsUnavailableException:
            if time.time() + every_s > deadline:
                raise
            sleep(every_s)


def _decimals(item: dict) -> dict:
    """boto3 takes numbers for DynamoDB as Decimal, not float."""
    from decimal import Decimal

    return {k: Decimal(str(v)) if isinstance(v, float) else v for k, v in item.items()}


def write_items(table, items: list[dict]) -> None:
    with table.batch_writer() as w:
        for it in items:
            w.put_item(Item=_decimals(it))


def create_failure_queue(sqs, s: Settings, sleep=time.sleep) -> tuple[str, str]:
    """SQS standard queue: where Lambda sends a batch it gave up on, so one bad record does not hold up the shard.
    It holds where the records are (shard, sequence numbers), not the records. 0.40 USD per million requests;
    nothing while empty. Kept 14 days (the maximum), so there is time to look. Returns (url, arn).

    SQS refuses a queue name for 60 s after a queue of that name was deleted (AWS docs, DeleteQueue): a run right
    after clean_up.py waits it out, 10 s at a time."""
    for attempt in range(8):
        try:
            url = sqs.create_queue(QueueName=s.failure_queue, Attributes={"MessageRetentionPeriod": "1209600"},
                                   tags={"recipe": "zero-etl-search"})["QueueUrl"]
            break
        except sqs.exceptions.QueueDeletedRecently:
            if attempt == 7:
                raise
            sleep(10)
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


def policies(name: str, index: str | list[str], writers: list[str], readers: list[str]) -> dict:
    """The three policies a collection needs, each naming this collection only (no `*` in any resource).
    `index` may be a list: during a move from items-v1 to items-v2 both are named (README, Adapt it)."""
    indexes = [index] if isinstance(index, str) else list(index)
    rules_for = lambda permissions: [  # noqa: E731
        {"ResourceType": "index", "Resource": [f"index/{name}/{i}" for i in indexes], "Permission": permissions}]
    data = []
    if writers:   # the indexing function: write and delete documents in these indexes, nothing else
        data.append({"Description": "indexing function writes", "Rules": rules_for(["aoss:WriteDocument"]),
                     "Principal": writers})
    if readers:   # you (the identity running the scripts): create the index, read its mapping, search, and
                  # delete an old index after a move (main/migrate.py drop)
        data.append({"Description": "owner creates and reads", "Rules": rules_for(
            ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:DeleteIndex"]) + [
            {"ResourceType": "collection", "Resource": [f"collection/{name}"],
             "Permission": ["aoss:DescribeCollectionItems"]}], "Principal": readers})
    return {
        # Encryption at rest with a key AWS owns: no KMS key to pay for or manage.
        "encryption": {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}],
                       "AWSOwnedKey": True},
        # Public endpoint; every request still needs a SigV4 signature from a principal in the data policy and
        # aoss:APIAccessAll in IAM. A team service would use a VPC endpoint instead (GENSEC01-BP02).
        "network": [{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}],
                     "AllowFromPublic": True}],
        "data": data,
    }


def merge_data_policy(current: list, new: list, drop: tuple[str, ...] = (),
                      drop_principals: tuple[str, ...] = ()) -> list:
    """`new` with what `current` already grants kept: per statement (matched by Description), the principals and
    the resources of each rule are the union of both. So a teammate's run adds their role instead of replacing
    yours, and a run with INDEX_VERSION=2 adds items-v2 beside items-v1. It only adds: `drop` removes resources by
    name (main/migrate.py drop) and `drop_principals` removes principals (main/migrate.py drop-principal), here and
    in statements only `current` has, which are otherwise kept as they are. A statement left with no rules or no
    principals is left out (the service refuses an empty one)."""
    by_desc = {st.get("Description"): st for st in current}
    out = []
    for st in new:
        old = by_desc.pop(st.get("Description"), {"Rules": [], "Principal": []})
        rules = []
        for rule in st["Rules"]:
            match = next((r for r in old["Rules"] if r["ResourceType"] == rule["ResourceType"]
                          and sorted(r["Permission"]) == sorted(rule["Permission"])), {"Resource": []})
            rules.append({**rule, "Resource": list(dict.fromkeys(match["Resource"] + rule["Resource"]))})
        out.append({**st, "Rules": rules, "Principal": list(dict.fromkeys(old["Principal"] + st["Principal"]))})
    out += list(by_desc.values())
    kept = []
    for st in out:
        rules = [{**r, "Resource": [x for x in r["Resource"] if x not in drop]} for r in st["Rules"]]
        rules = [r for r in rules if r["Resource"]]
        principals = [p for p in st["Principal"] if p not in drop_principals]
        if rules and principals:
            kept.append({**st, "Rules": rules, "Principal": principals})
    return kept


def put_data_policy(aoss, name: str, policy: list, drop: tuple[str, ...] = ()) -> list:
    """Create the data access policy, or merge into it when it exists (merge_data_policy). Returns the policy."""
    try:
        aoss.create_access_policy(name=name, type="data", policy=json.dumps(policy))
        return policy
    except aoss.exceptions.ConflictException:
        current = aoss.get_access_policy(name=name, type="data")["accessPolicyDetail"]
        merged = merge_data_policy(current["policy"], policy, drop)
        if merged != current["policy"]:
            aoss.update_access_policy(name=name, type="data", policy=json.dumps(merged),
                                      policyVersion=current["policyVersion"])
        return merged


def create_collection(aoss, s: Settings, name: str, kind: str, writers: list[str], readers: list[str] | None = None,
                      index: str | None = None, wait_s: int = 900, group: str | None = None) -> str:
    """One collection in its group (default s.group); returns its endpoint. kind: SEARCH (keyword) or VECTORSEARCH.
    A collection group holds collections of one type only (AWS docs, "Managing capacity limits for Amazon OpenSearch
    Serverless", https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scaling.html), so
    ext-embeddings' VECTORSEARCH collection goes into a group of its own (s.vector_group).
    `writers` may write documents, `readers` may create the index and search; with no readers, the writers do both
    (ext-embeddings, where you write the vectors yourself)."""
    def ignore_conflict(fn, **kw):
        try:
            fn(**kw)
        except aoss.exceptions.ConflictException:
            pass

    readers = writers if readers is None else readers
    p = policies(name, index or s.index, writers, readers)
    ignore_conflict(aoss.create_security_policy, name=name, type="encryption", policy=json.dumps(p["encryption"]))
    ignore_conflict(aoss.create_security_policy, name=name, type="network", policy=json.dumps(p["network"]))
    put_data_policy(aoss, name, p["data"])
    # Collection group, NextGen: the unit that holds capacity and is billed. Minimum 0 OCU (OpenSearch Compute
    # Unit) for indexing and for search, so after 10 minutes with no requests to any collection in the group
    # both drop to zero and billing for them stops; the first request after that waits 10 to 30 s (AWS docs,
    # "Scale to zero"). Maximum 2 OCU each: the smallest maximum the API takes (MAX_OCU above), so a runaway
    # loop cannot cost more than 4 x 0.266 USD an hour. Only NextGen groups scale to zero; a Classic group cannot go
    # below 1 OCU (AWS docs, "Collection group capacity limits").
    # Standby replicas ENABLED: a copy in a second Availability Zone. A NextGen group refuses DISABLED (measured,
    # 2026-11-12 sandbox run), so there is no cheaper choice to make here; the OCU maximum still caps the bill.
    group = group or s.group

    def create_group():
        ignore_conflict(aoss.create_collection_group, name=group, generation="NEXTGEN", standbyReplicas="ENABLED",
                        capacityLimits={"minIndexingCapacityInOCU": MIN_OCU, "minSearchCapacityInOCU": MIN_OCU,
                                        "maxIndexingCapacityInOCU": MAX_OCU, "maxSearchCapacityInOCU": MAX_OCU},
                        tags=[{"key": "recipe", "value": "zero-etl-search"}])

    create_group()
    deadline = time.time() + wait_s
    # A collection that clean_up.py is still deleting answers "already exists" to a create, and then disappears:
    # wait until it is gone, so the create below makes a new one.
    while _status(aoss, name) == "DELETING" and time.time() < deadline:
        time.sleep(15)
    # The collection: SEARCH type (full-text search, documents kept by the id we give them). Storage is billed per
    # GB-month; a few kilobytes here. It uses the group's OCUs, not its own.
    # A group an earlier clean-up was deleting can answer "already exists" above and then be gone: the collection
    # create then names a group that does not exist. Make the group again and retry until the deadline. (The API
    # reports no status for a group, so "being deleted" cannot be read: CollectionGroupDetail,
    # https://docs.aws.amazon.com/opensearch-service/latest/ServerlessAPIReference/API_CollectionGroupDetail.html)
    while True:
        try:
            ignore_conflict(aoss.create_collection, name=name, type=kind, standbyReplicas="ENABLED",
                            collectionGroupName=group, tags=[{"key": "recipe", "value": "zero-etl-search"}])
            break
        except ClientError as err:
            if not group_gone(err, group) or time.time() > deadline:
                raise
            time.sleep(15)
            create_group()
    while time.time() < deadline:
        details = aoss.batch_get_collection(names=[name])["collectionDetails"]
        if details and details[0]["status"] == "ACTIVE":
            return details[0]["collectionEndpoint"]
        if details and details[0]["status"] == "FAILED":
            raise RuntimeError(f"collection {name} failed: {details[0]}")
        time.sleep(15)
    raise TimeoutError(f"collection {name} not active after {wait_s} s")


def group_gone(err: ClientError, group: str) -> bool:
    """True when a create_collection refusal says its collection group does not exist: ResourceNotFoundException,
    or a ValidationException whose message names the group. Any other ValidationException is a real mistake."""
    code, message = err.response["Error"]["Code"], err.response["Error"].get("Message", "")
    return code == "ResourceNotFoundException" or (
        code == "ValidationException" and ("collection group" in message.lower() or group in message))


def _status(aoss, name: str) -> str:
    details = aoss.batch_get_collection(names=[name])["collectionDetails"]
    return details[0]["status"] if details else ""


def collection_arn(aoss, name: str) -> str:
    return aoss.batch_get_collection(names=[name])["collectionDetails"][0]["arn"]


def client_for(endpoint: str, region: str):
    """An OpenSearch client for you (the scripts and the demo), signed with SigV4 for `aoss`."""
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss")
    return OpenSearch(hosts=[{"host": endpoint.removeprefix("https://"), "port": 443}], http_auth=auth,
                      use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=60)


def response_hint(endpoint: str, region: str) -> str:
    """One signed GET to the collection; returns "<status> <x-aoss-response-hint>". OpenSearch Serverless says which
    policy refused a request in that header: X01:network-policy-deny with a 401, X01:gw-helper-deny (data access
    policy) with a 403 (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html)."""
    import hashlib

    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    req = AWSRequest(method="GET", url=endpoint.rstrip("/") + "/_cat/indices",
                     headers={"x-amz-content-sha256": hashlib.sha256(b"").hexdigest()})
    SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(), "aoss", region).add_auth(req)
    try:
        r = requests.get(req.url, headers=dict(req.headers.items()), timeout=20)
    except requests.RequestException as err:
        return f"no answer ({type(err).__name__})"
    return f"{r.status_code} {r.headers.get('x-aoss-response-hint', '(no hint header)')}"


def create_index(client, name: str, body: dict, wait_s: int = 300, every_s: float = 10, log=print,
                 since: float | None = None, hint=None) -> dict:
    """Create the versioned index before the function writes to it; return the recorded version (_meta).

    Right after a collection is created its policies may not apply yet: a 401 (network policy) or 403 (data access
    policy) (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html).
    The AWS docs name no time for a new policy to take effect; on 2026-11-13 every request in the first 70 s after
    the collection was ACTIVE got a 401 (output/main-aws-2026-11-13-refused.txt). So 401 and 403 are tried again
    every `every_s` seconds for up to `wait_s` (ours); each try is logged with the seconds since `since` (the
    collection became ACTIVE) and, with `hint`, the policy the service names."""
    from opensearchpy.exceptions import AuthenticationException, AuthorizationException

    since = since or time.time()
    deadline = time.time() + wait_s
    while True:
        try:
            if not client.indices.exists(index=name):
                client.indices.create(index=name, body=body)
            meta = client.indices.get_mapping(index=name)[name]["mappings"]["_meta"]
            log(f"collection let you in {time.time() - since:.0f} s after it was ACTIVE")
            return meta
        except (AuthenticationException, AuthorizationException) as err:
            if time.time() + every_s > deadline:
                raise
            why = f"{err.status_code}; a GET of _cat/indices answers {hint()}" if hint else str(err.status_code)
            log(f"{time.time() - since:.0f} s after ACTIVE: refused ({why}); trying again in {every_s:.0f} s")
            time.sleep(every_s)


def function_zip() -> bytes:
    """main/handler.py alone, as handler.py: it needs nothing beyond the runtime's boto3 and botocore."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "main" / "handler.py", "handler.py")
    return buf.getvalue()


def create_log_group(logs, s: Settings) -> None:
    """The function's log group, made before its first run so it keeps logs 1 day, not forever (Lambda would make
    it with no expiry). CloudWatch Logs bills per GB ingested; one line per batch here."""
    try:
        logs.create_log_group(logGroupName=s.log_group, tags={"recipe": "zero-etl-search"})
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=s.log_group, retentionInDays=1)


def create_function(lam, s: Settings, role: str, endpoint: str) -> str:
    """The indexing function. Python 3.13 on arm64 (Graviton: 20 % cheaper per GB-second than x86 in the price
    list). 128 MB, a 60 s timeout (FUNCTION_TIMEOUT_S). Billed per request (0.20 USD per million) and per GB-second; a few hundred calls
    here cost under 0.01 USD. Returns its ARN. A second run updates the code and settings in place."""
    env = {"Variables": {"ENDPOINT": endpoint, "INDEX": s.index, "INDEX_VERSION": str(s.index_version)}}
    try:
        arn = lam.create_function(
            FunctionName=s.function, Runtime="python3.13", Architectures=["arm64"], Role=role,
            Handler="handler.handler", Code={"ZipFile": function_zip()}, Timeout=FUNCTION_TIMEOUT_S,
            MemorySize=FUNCTION_MEMORY_MB, Environment=env, Tags={"recipe": "zero-etl-search"})["FunctionArn"]
    except lam.exceptions.ResourceConflictException:
        lam.get_waiter("function_active_v2").wait(FunctionName=s.function)
        arn = lam.update_function_code(FunctionName=s.function, ZipFile=function_zip())["FunctionArn"]
        lam.get_waiter("function_updated_v2").wait(FunctionName=s.function)
        lam.update_function_configuration(FunctionName=s.function, Role=role, Environment=env,
                                          Timeout=FUNCTION_TIMEOUT_S, MemorySize=FUNCTION_MEMORY_MB)
        lam.get_waiter("function_updated_v2").wait(FunctionName=s.function)
    lam.get_waiter("function_active_v2").wait(FunctionName=s.function)
    return arn


MAPPING_SETTINGS = dict(
    BatchSize=BATCH_SIZE, MaximumBatchingWindowInSeconds=BATCH_WINDOW_S,
    # Split a batch in two when the function raises; splitting does not use up retries
    # (https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html).
    BisectBatchOnFunctionError=True,
    # The function reports the earliest record it could not write; Lambda checkpoints before it and sends the rest
    # again (https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html).
    FunctionResponseTypes=["ReportBatchItemFailures"],
    MaximumRetryAttempts=MAX_RETRIES, MaximumRecordAgeInSeconds=MAX_RECORD_AGE_S,
)


def create_mapping(lam, s: Settings, stream_arn: str, queue_arn: str, wait_s: int = 300, log=print) -> str:
    """The event source mapping: Lambda reads the stream and calls the function with batches, in order per shard.

    TRIM_HORIZON: start at the oldest record the stream still has (24 hours), so items written before the mapping
    was ready are indexed too. A record the index refuses is split off (bisect) and, after MAX_RETRIES, sent alone
    to the failure queue; the shard moves on. A mapping from an earlier run gets the current settings, and is
    enabled again if it was disabled. Returns the mapping's UUID once it is Enabled."""
    found = lam.list_event_source_mappings(FunctionName=s.function, EventSourceArn=stream_arn)["EventSourceMappings"]
    found = [m for m in found if m.get("State") != "Deleting"]
    destination = {"OnFailure": {"Destination": queue_arn}}
    if found:
        uuid = found[0]["UUID"]
        if found[0].get("State") in ("Disabled", "Disabling"):
            log(f"event source mapping {uuid} was {found[0]['State']}: enabling it again")
        while lam.get_event_source_mapping(UUID=uuid)["State"] in ("Creating", "Updating", "Enabling", "Disabling"):
            time.sleep(5)                       # an update is refused while another change is in progress
        lam.update_event_source_mapping(UUID=uuid, Enabled=True, DestinationConfig=destination, **MAPPING_SETTINGS)
    else:
        uuid = lam.create_event_source_mapping(
            FunctionName=s.function, EventSourceArn=stream_arn, StartingPosition="TRIM_HORIZON",
            DestinationConfig=destination, Tags={"recipe": "zero-etl-search"}, **MAPPING_SETTINGS)["UUID"]
    deadline = time.time() + wait_s
    while time.time() < deadline:
        state = lam.get_event_source_mapping(UUID=uuid)["State"]
        if state == "Enabled":
            return uuid
        time.sleep(5)
    raise TimeoutError(f"event source mapping {uuid} not Enabled after {wait_s} s")


def wait_until_function_can_write(lam, s: Settings, wait_s: int = 300, every_s: float = 10, log=print) -> str:
    """Invoke the function's probe (one write and one delete of a probe document, main/handler.py probe) until it
    works: a new IAM statement and a new data access policy take a while to reach every endpoint (IAM docs,
    https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot.html#troubleshoot_general_eventual-consistency),
    and a collection at zero capacity takes 10 to 30 s to answer
    (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html). Only
    then does the mapping start, so the first batches do not spend their retries on that wait."""
    deadline, last = time.time() + wait_s, ""
    while time.time() < deadline:
        resp = lam.invoke(FunctionName=s.function, Payload=json.dumps({"probe": True}).encode())
        out = json.loads(resp["Payload"].read() or b"{}")
        if not resp.get("FunctionError") and out.get("ok"):
            return out.get("detail", "")
        last = str(out)[:300]
        log(f"function cannot write yet ({last}); trying again in {every_s:.0f} s")
        time.sleep(every_s)
    raise TimeoutError(f"the function could not write to the index after {wait_s} s: {last}")


def create_alarms(cw, s: Settings) -> list[str]:
    """Two CloudWatch alarms, no action attached: they show in the console and in `aws cloudwatch describe-alarms`
    (Serverless Applications Lens, "Metrics and alerts": for stream-based invocations, alert on IteratorAge).
    0.10 USD per alarm-month in ca-central-1 (AWS bulk price list, read 2026-11-13)."""
    # The age of the oldest record the function has not finished. A refused record held its shard for 2 min 45 s
    # while Lambda retried it (2026-11-13), so 5 minutes means the sync is stuck, not one bad record.
    cw.put_metric_alarm(
        AlarmName=s.alarm_iterator_age, Namespace="AWS/Lambda", MetricName="IteratorAge",
        Dimensions=[{"Name": "FunctionName", "Value": s.function}], Statistic="Maximum", Period=60,
        EvaluationPeriods=1, Threshold=ITERATOR_AGE_ALARM_MS, ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching", Tags=[{"Key": "recipe", "Value": "zero-etl-search"}])
    # Any message in the failure queue is a record the index does not hold: fix the item in the table.
    cw.put_metric_alarm(
        AlarmName=s.alarm_failed_records, Namespace="AWS/SQS", MetricName="ApproximateNumberOfMessagesVisible",
        Dimensions=[{"Name": "QueueName", "Value": s.failure_queue}], Statistic="Maximum", Period=60,
        EvaluationPeriods=1, Threshold=1, ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching", Tags=[{"Key": "recipe", "Value": "zero-etl-search"}])
    return [s.alarm_iterator_age, s.alarm_failed_records]


def create_all(s: Settings, items: list[dict]) -> dict:
    """Everything, in the order each part needs the one before. Returns what the scenario uses."""
    account = account_id(s.region)
    me = caller_principal(s.region)
    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    stream_arn = create_table(c("dynamodb"), s)
    queue_url, queue_arn = create_failure_queue(c("sqs"), s)
    role_name = f"{s.prefix}{ROLE_SUFFIXES[0]}"
    role = ensure_role(c("iam"), role_name, "lambda.amazonaws.com",
                       indexer_statements(s, account, stream_arn, queue_arn), s.lambda_role_arn)
    aoss = c("opensearchserverless")
    endpoint = create_collection(aoss, s, s.collection, "SEARCH", writers=[role], readers=[me])
    active_at = time.time()
    role = ensure_role(c("iam"), role_name, "lambda.amazonaws.com",   # now with the collection's ARN
                       indexer_statements(s, account, stream_arn, queue_arn, collection_arn(aoss, s.collection)),
                       s.lambda_role_arn)
    client = client_for(endpoint, s.region)
    meta = create_index(client, s.index, index_body(s), since=active_at,
                        hint=lambda: response_hint(endpoint, s.region))
    create_log_group(c("logs"), s)
    create_function(c("lambda"), s, role, endpoint)
    wait_until_function_can_write(c("lambda"), s)
    mapping = create_mapping(c("lambda"), s, stream_arn, queue_arn)
    create_alarms(c("cloudwatch"), s)
    # The items go in last: their stream records are read from TRIM_HORIZON, so they would be indexed in any
    # order, but now the scenario can time them.
    write_items(boto3.resource("dynamodb", region_name=s.region).Table(s.table), items)
    return {"account": account, "endpoint": endpoint, "client": client, "meta": meta, "stream_arn": stream_arn,
            "queue_url": queue_url, "mapping": mapping}
