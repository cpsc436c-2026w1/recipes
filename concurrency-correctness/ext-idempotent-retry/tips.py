"""A rider adds a tip to a trip. The write succeeds, the reply is lost to a timeout, and the app retries.

- add_tip_naive: UpdateItem ADD tip_total. Each attempt that reaches DynamoDB adds the tip again.
- add_tip_once: the app makes one request_id when the rider taps (uuid4) and sends it on every attempt. One
  transaction writes a marker item with that request_id to the requests table, on the condition that it does not
  exist yet, and adds the tip.
  A retry finds the marker, the whole transaction is refused, and the tip is added once (DDIA 2e ch. 13,
  "Uniquely identifying requests"; REL04-BP04).

The transaction also carries ClientRequestToken = request_id. DynamoDB treats a repeat with the same token within
10 minutes as already done, so a retry inside that window succeeds without a change. The marker covers what the token
does not: a retry after 10 minutes, or a retry that changed the request. (moto ignores the token, so the offline tests
exercise the marker.)

README "Decisions" explains each choice.
"""

import random
import time
import uuid

from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from trips import CEILING        # the same ceiling on throughput as the trips table (main/trips.py)

TIMEOUTS = (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError)
MARKER_DAYS = 7


def new_request_id() -> str:
    """Made once, on the phone, when the rider taps; reused on every retry of that tap."""
    return str(uuid.uuid4())


def add_tip_naive(ddb, table: str, trip_id: str, amount: int, request_id: str | None = None) -> str:
    ddb.update_item(TableName=table, Key={"trip_id": {"S": trip_id}}, UpdateExpression="ADD tip_total :a",
                    ExpressionAttributeValues={":a": {"N": str(amount)}})
    return "applied"


def requests_table_name(prefix: str) -> str:
    return f"{prefix}-race-requests"


def create_requests_table(ddb, name: str, poll_s: float = 2.0) -> None:
    """Key request_id; markers expire after MARKER_DAYS through the table's time to live on expires_at."""
    for _ in range(2):
        try:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", OnDemandThroughput=CEILING,
                             KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
                             Tags=[{"Key": "purpose", "Value": "recipe-concurrency-correctness"}])
            break
        except ddb.exceptions.ResourceInUseException:
            # ResourceInUseException is the answer for a table that is there (API reference, CreateTable). We also
            # take it for one that is still being deleted (a clean-up that was stopped before it finished), which
            # that page does not say: that one is waited out and created again.
            try:
                status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
            except ddb.exceptions.ResourceNotFoundException:
                continue
            if status != "DELETING":
                break
            ddb.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})
    # DynamoDB refuses to enable time to live a second time (a run over a table kept with --keep): ask first
    ttl = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
        ddb.update_time_to_live(TableName=name,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})


def add_tip_once(ddb, table: str, trip_id: str, amount: int, request_id: str, requests_table: str) -> str:
    """Returns 'applied' the first time, 'already applied' for a repeat of the same request_id."""
    marker = {"Put": {
        "TableName": requests_table,
        "Item": {"request_id": {"S": request_id}, "trip_id": {"S": trip_id}, "amount": {"N": str(amount)},
                 "expires_at": {"N": str(int(time.time()) + MARKER_DAYS * 86400)}},
        "ConditionExpression": "attribute_not_exists(request_id)"}}
    tip = {"Update": {
        "TableName": table, "Key": {"trip_id": {"S": trip_id}},
        "UpdateExpression": "ADD tip_total :a",
        "ConditionExpression": "attribute_exists(trip_id)",          # never create a trip by tipping it
        "ExpressionAttributeValues": {":a": {"N": str(amount)}}}}
    try:
        ddb.transact_write_items(TransactItems=[marker, tip], ClientRequestToken=request_id)
        return "applied"
    except ddb.exceptions.TransactionCanceledException as err:
        reasons = err.response.get("CancellationReasons", [])
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return "already applied"
        raise


def send_with_retries(call, attempts: int = 3, backoff_s: float = 0.2, log=print) -> str:
    """What the app does on a timeout: it cannot tell a lost request from a lost reply, so it sends again."""
    for attempt in range(1, attempts + 1):
        try:
            result = call()
            log(f"  attempt {attempt}: {result}")
            return result
        except TIMEOUTS as err:
            if attempt == attempts:
                log(f"  attempt {attempt}: timed out ({type(err).__name__}), giving up")
                break                                                  # no wait before giving up
            log(f"  attempt {attempt}: timed out ({type(err).__name__}), retrying")
            # Exponential backoff with jitter: a random wait of at most backoff_s, then at most twice that, and so
            # on. Many apps that lost their replies at the same moment then come back spread out, not together.
            time.sleep(random.uniform(0, backoff_s * 2 ** (attempt - 1)))
    raise RuntimeError(f"no reply after {attempts} attempts")


class ReplyLostOnce:
    """Wraps a DynamoDB client: the first write reaches DynamoDB and is applied, then its reply is lost.

    This is the case a retry must survive. A real timeout looks the same to the app: botocore raises ReadTimeoutError
    when no reply comes within read_timeout, and the write may or may not have been applied.
    """

    def __init__(self, client, operations=("update_item", "transact_write_items")):
        self._client = client
        self._ops = set(operations)
        self.exceptions = client.exceptions
        self.lost = 0

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in self._ops:
            return attr

        def call(*args, **kwargs):
            result = attr(*args, **kwargs)                 # applied in the table
            if self.lost == 0:
                self.lost += 1
                raise ReadTimeoutError(endpoint_url="https://dynamodb.ca-central-1.amazonaws.com")
            return result
        return call
