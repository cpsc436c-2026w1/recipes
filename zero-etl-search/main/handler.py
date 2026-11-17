"""The indexing function: DynamoDB stream records in, _bulk requests to the OpenSearch Serverless index out.

This file is the whole function package: main/infra.py zips it alone. It imports only the Python standard library
and boto3/botocore, which the Lambda Python runtime already has, so there is nothing to package beside it and the
zip stays one file. (Lambda's own guidance is to package the SDK version you test with; for a recipe this small we
use the runtime's copy, and the offline tests run against the boto3 in uv.lock.)

Environment (set by main/infra.py): ENDPOINT (the collection's https endpoint), INDEX (items-v1), INDEX_VERSION.

What it does with each record (README, Decisions):
  INSERT, MODIFY  index the new image under the item's key: document id = item key, so writing the same change
                  twice (a retried batch) leaves one document with the same content
  REMOVE          delete the document with that id; a delete of a document that is not there is not an error

Two kinds of failure, handled differently:
  temporary   the collection waking from zero, a 403 while a new IAM statement or data access policy spreads, a
              429, a 5xx, a timeout, or a 429/5xx for one item in the _bulk answer. Retried here, with backoff,
              for as long as the function's time allows. Still failing then: the function reports the earliest
              unwritten record (ReportBatchItemFailures) and Lambda tries the batch again from there, up to the
              mapping's MaximumRetryAttempts. Nothing is split, so good records are not set aside.
  refused     OpenSearch answers 4xx for a document (a value the mapping cannot take, say). Trying again gives the
              same answer. The function reports that record; when it is the first of its batch, it raises instead,
              so Lambda splits the batch (BisectBatchOnFunctionError) until the record is alone, and after the
              retries only that one record goes to the failure queue.
AWS docs for both: https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer

KEY = "id"                 # the table's partition key (main/infra.py create_table); Adapt it: your key
MAX_ATTEMPTS = 8           # ours: _bulk calls per invocation, at most; with the backoff below about 30 s in all
BACKOFF_S = (0.5, 1, 2, 4, 8)   # ours: the longest wait before each new attempt (the last value repeats); the
                                # wait is a random part of it, between half and all, so retries from several
                                # shards do not arrive together (REL05-BP03: backoff, jitter, a limit,
                                # https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html)
SAFETY_S = 5               # ours: stop retrying this long before the function's timeout
# HTTP answers that mean "try again later" (the whole request, or one item of a _bulk answer). 403 is in the list
# because a new IAM statement or data access policy takes a while to reach every endpoint (IAM docs, "Changes that
# I make are not always immediately visible",
# https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot.html#troubleshoot_general_eventual-consistency);
# a 403 that does not clear ends in the failure queue after the mapping's retries, like any other.
TEMPORARY = {403, 408, 429, 500, 502, 503, 504}
_deser = TypeDeserializer()


class BatchFailed(Exception):
    """Raised so Lambda splits the batch: a refused record first in its batch ends up alone, then in the queue."""


class Temporary(Exception):
    """A request that may work if sent again (TEMPORARY statuses, timeouts, connection errors)."""


def _plain(v):
    """DynamoDB numbers arrive as Decimal; JSON wants int or float."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, set, tuple)):
        return [_plain(x) for x in v]
    return v


def _item(image: dict) -> dict:
    return {k: _plain(_deser.deserialize(v)) for k, v in image.items()}


def actions(records: list[dict], index: str, index_version: int, now: float) -> list[tuple[str, str, dict | None]]:
    """(action, document id, source) per item, in stream order; see positioned() for where each came from."""
    return [a for _, a in positioned(records, index_version, now)]


def positioned(records: list[dict], index_version: int, now: float) -> list[tuple[int, tuple[str, str, dict | None]]]:
    """(position in the batch, (action, document id, source)), one per item, in stream order.

    Only the last change of each item in the batch is kept: the stream gives one item's changes in the order they
    happened (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html), so the last one is
    the item as the table has it now. Two changes to one id never sit in one bulk request.
    """
    last: dict[str, tuple[int, tuple[str, str, dict | None]]] = {}
    for pos, r in enumerate(records):
        d = r["dynamodb"]
        doc_id = str(_item(d["Keys"])[KEY])
        last.pop(doc_id, None)
        if r["eventName"] == "REMOVE":
            last[doc_id] = (pos, ("delete", doc_id, None))
            continue
        source = _item(d["NewImage"])
        source.pop(KEY, None)                     # the key is the document id; no need to keep it twice
        source.update({
            "index_version": index_version,        # which mapping version wrote this document
            "changed_at": d.get("ApproximateCreationDateTime"),  # when the table change happened (stream time)
            "indexed_at": round(now, 3),           # when this function sent it; README, What you should see: lag
        })
        last[doc_id] = (pos, ("index", doc_id, source))
    return list(last.values())


def bulk_body(acts: list[tuple[str, str, dict | None]], index: str) -> str:
    lines = []
    for action, doc_id, source in acts:
        lines.append(json.dumps({action: {"_index": index, "_id": doc_id}}))
        if source is not None:
            lines.append(json.dumps(source))
    return "\n".join(lines) + "\n"


def apply(records: list[dict], post, index: str, index_version: int, now=time.time, log=print, sleep=time.sleep,
          time_left=lambda: 60.0, jitter=random.random) -> dict:
    """Send one batch to the index through `post(path, body) -> parsed JSON`, retrying temporary failures.

    Returns {"batchItemFailures": [...]} for Lambda (empty when every record is written or deliberately skipped).
    Raises BatchFailed when the earliest record still unwritten is refused and is the first of the batch.
    `time_left()` is the seconds before the function's timeout (Lambda's context in handler()).
    """
    todo = positioned(records, index_version, now())
    counts = {"records": len(records), "indexed": 0, "deleted": 0, "refused": [], "attempts": 0, "temporary": ""}
    refused: list[int] = []                         # positions of records the index refused
    attempt = 0
    while todo and attempt < MAX_ATTEMPTS:
        attempt += 1
        counts["attempts"] = attempt
        try:
            resp = post("/_bulk", bulk_body([a for _, a in todo], index))
            again = []
            for (pos, (action, doc_id, source)), item in zip(todo, resp.get("items", [])):
                result = item.get(action, {})
                status = result.get("status", 500)
                if status < 300 or (action == "delete" and status == 404):   # a replayed delete: already gone
                    counts["indexed" if action == "index" else "deleted"] += 1
                elif status in TEMPORARY:
                    again.append((pos, (action, doc_id, source)))
                    counts["temporary"] = f"item {status}"
                else:
                    reason = result.get("error", {})
                    refused.append(pos)
                    counts["refused"].append(f"{doc_id}: {reason.get('type', status)}: {reason.get('reason', '')}"[:300])
            todo = again + todo[len(resp.get("items", [])):]   # items the answer left out count as not written
        except Temporary as err:
            counts["temporary"] = str(err)[:200]
        if todo and attempt < MAX_ATTEMPTS:         # no wait after the last call: nothing comes after it
            wait = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)] * (0.5 + jitter() / 2)
            if time_left() - wait < SAFETY_S:
                break
            sleep(wait)
    log(json.dumps(counts))                          # one line per batch: Lambda sends it to CloudWatch Logs
    unwritten = sorted([pos for pos, _ in todo] + refused)
    if not unwritten:
        return {"batchItemFailures": []}
    first = unwritten[0]
    if first in refused and first == 0:
        raise BatchFailed(f"{len(refused)} documents refused: {counts['refused'][0]}")
    # Lambda takes the lowest sequence number as the checkpoint and sends the batch again from there.
    return {"batchItemFailures": [{"itemIdentifier": records[first]["dynamodb"]["SequenceNumber"]}]}


def signed_post(endpoint: str, region: str, credentials=None, timeout=lambda: 25.0):
    """`post(path, body)` that signs each request with SigV4 for the service `aoss` (OpenSearch Serverless), using
    botocore's signer and the function's role credentials. The x-amz-content-sha256 header is required by
    OpenSearch Serverless (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-clients.html#serverless-signing).
    Raises Temporary for an answer worth retrying, BatchFailed for any other refusal of the whole request."""
    import hashlib
    import socket

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    creds = credentials or Session().get_credentials()

    def post(path: str, body: str) -> dict:
        data = body.encode()
        req = AWSRequest(method="POST", url=endpoint.rstrip("/") + path, data=data, headers={
            "Content-Type": "application/x-ndjson", "x-amz-content-sha256": hashlib.sha256(data).hexdigest()})
        SigV4Auth(creds.get_frozen_credentials(), "aoss", region).add_auth(req)
        http = urllib.request.Request(req.url, data=data, headers=dict(req.headers.items()), method="POST")
        try:
            with urllib.request.urlopen(http, timeout=max(1.0, timeout())) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as err:
            text = f"HTTP {err.code} from the collection: {err.read()[:300]!r}"
            raise (Temporary(text) if err.code in TEMPORARY else BatchFailed(text)) from err
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as err:
            raise Temporary(f"{type(err).__name__}: {err}") from err

    return post


def probe(post) -> dict:
    """One write and one delete of a probe document, no retries: main/infra.py calls this through the deployed
    function before it starts the event source mapping, until the function's role can write (IAM and the data
    access policy have spread, the collection is awake)."""
    try:
        resp = post("/_bulk", bulk_body([("index", "__probe__", {"probe": True}), ("delete", "__probe__", None)],
                                        os.environ["INDEX"]))
    except (Temporary, BatchFailed) as err:
        return {"ok": False, "detail": str(err)[:300]}
    statuses = [next(iter(i.values())).get("status", 500) for i in resp.get("items", [])]
    return {"ok": bool(statuses) and all(s < 300 or s == 404 for s in statuses), "detail": str(statuses)}


_post = None
_left = lambda: 60.0  # noqa: E731 - seconds before this invocation's timeout; set per invocation below


def handler(event, context):
    global _post, _left
    _left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else (lambda: 60.0)
    if _post is None:                              # one signer per execution environment, made on first use
        _post = signed_post(os.environ["ENDPOINT"], os.environ.get("AWS_REGION", "ca-central-1"),
                            timeout=lambda: min(25.0, _left() - SAFETY_S))
    if event.get("probe"):
        return probe(_post)
    return apply(event["Records"], _post, os.environ["INDEX"], int(os.environ.get("INDEX_VERSION", "1")),
                 time_left=_left)
