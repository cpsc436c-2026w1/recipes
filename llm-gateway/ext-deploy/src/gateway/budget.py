"""Rate limit, reserve before the call, settle after it.

Overshoot bound. A key cannot pass its budget by more than the gap between the
reserve and the upstream's own count. The reserve is charged before the upstream sees the
request and it is an upper bound on the cost. Output is capped by max_tokens
(injected when absent, clipped to the key's cap, and max_completion_tokens is
clipped the same way). Requests with n other than 1 or with best_of are refused.
Input is counted at one token per byte plus 16 per message.

A failed request. When the upstream never received it (the request could not be
built, or the connection failed), the whole reserve goes back. When it had been
sent, a timeout or a 5xx keeps the whole reserve as spent: the provider may have
generated up to max_tokens and may bill them. A 4xx from the upstream (it refused
the request) gives the reserve back.

Where the count can be wrong. A function killed by its timeout never settles, so
the reserve stays as spent: the key is charged more than it used. A stream without
a usage chunk settles on an estimate that can be lower than what the provider
bills (README, Known limits).
"""

import json
import time
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from . import aws, pricing, settings
from .auth import GatewayError
from .settings import TOKENS_PER_MESSAGE

_deser = TypeDeserializer()


def utc_day(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y-%m-%d")


def rate_limit(key_id: str, max_rpm: int, now: float | None = None) -> None:
    """Count one request in this key's current minute. Over the limit: 429."""
    # Decision (rate, ../README.md): a per-key counter per clock minute, checked by a conditional update, outside the
    # caller's code path: a loop in a student's code is stopped here, whatever the loop does.
    now = time.time() if now is None else now
    minute = int(now // 60)
    try:
        aws.usage_table().update_item(
            Key={"key_id": key_id, "sk": f"min#{minute}"},
            UpdateExpression="ADD #c :one SET #t = :ttl",
            ConditionExpression="attribute_not_exists(#c) OR #c < :max",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": minute * 60 + 7200, ":max": max_rpm},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise GatewayError(
                429, f"rate limit of {max_rpm} requests per minute reached", "rate_limit_error",
                "rate_limit_exceeded", headers={"Retry-After": "60"},
            ) from None
        raise


def input_tokens_estimate(body: dict, embeddings: bool) -> int:
    """Upper bound on input tokens: one per byte, plus 16 per message or input."""
    total = 0
    if embeddings:
        inp = body.get("input", "")
        items = inp if isinstance(inp, list) else [inp]
        for x in items:
            total += len((x if isinstance(x, str) else json.dumps(x)).encode()) + TOKENS_PER_MESSAGE
        return total
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise GatewayError(400, "messages must be a list", "invalid_request_error", "bad_messages")
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else m
        text = content if isinstance(content, str) else json.dumps(content)
        total += len(text.encode()) + TOKENS_PER_MESSAGE
        # A message carries text outside `content` too (an assistant message's tool_calls, a name): the model
        # reads it, so the bound counts it. Without this a large tool call is reserved as 16 tokens.
        rest = {k: v for k, v in m.items() if k not in ("role", "content")} if isinstance(m, dict) else {}
        if rest:
            total += len(json.dumps(rest).encode())
    for extra in ("tools", "functions", "response_format"):
        if extra in body:
            total += len(json.dumps(body[extra]).encode())
    return total


def output_tokens_bound(body: dict) -> int:
    """Largest output the request allows, after the gateway clipped it.

    Both caps sent: the upstream may honour either one, so the larger counts.
    """
    vals = [int(body[k]) for k in ("max_tokens", "max_completion_tokens") if body.get(k) is not None]
    return max(vals) if vals else 0


def reserve_amount(model: str, tokens_in_est: int, tokens_out_max: int) -> int:
    return pricing.cost_in(model, tokens_in_est) + pricing.cost_out(model, tokens_out_max)


def reserve(item: dict, model: str, amount: int, now: int | None = None) -> None:
    """Charge the reserve with one conditional update.

    Decision (budget, ../README.md): reserve the worst case before the call and settle after it, in one conditional
    write, so requests running at the same time cannot together spend past the budget.

    The condition holds no arithmetic: `:cap` is budget minus reserve, computed
    here from the budget the GetItem just read. Admin keys have no budget
    and skip the spent clause.
    """
    now = int(time.time()) if now is None else now
    cond = "#state = :active AND expires_at > :now AND contains(allowed_models, :model)"
    values = {":reserve": amount, ":one": 1, ":now": now, ":active": "active", ":model": model}
    budget = item.get("budget_micro")
    if budget is not None:
        cond += " AND spent_micro <= :cap"
        values[":cap"] = int(budget) - amount
    try:
        aws.keys_table().update_item(
            Key={"key_hash": item["key_hash"]},
            UpdateExpression="ADD spent_micro :reserve, requests :one SET last_used_at = :now",
            ConditionExpression=cond,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raw = e.response.get("Item")
        old = {k: _deser.deserialize(v) for k, v in raw.items()} if raw else item
        _refusal(old, model, now)


def _refusal(old: dict, model: str, now: int) -> None:
    if old.get("state") != "active":
        raise GatewayError(403, "this key has been revoked", "permission_error", "key_revoked")
    if int(old.get("expires_at", 0)) <= now:
        raise GatewayError(403, "this key has expired", "permission_error", "key_expired")
    allowed = set(old.get("allowed_models", set()))
    if model not in allowed:
        raise GatewayError(
            403, f"model {model!r} is not allowed for this key. Allowed: {', '.join(sorted(allowed))}",
            "permission_error", "model_not_allowed",
        )
    spent = pricing.micro_to_amount(int(old.get("spent_micro", 0)))
    budget = pricing.micro_to_amount(int(old.get("budget_micro", 0)))
    raise GatewayError(
        429,
        f"budget spent: {spent} of {budget} {settings.CURRENCY} used. Ask the person who issued your key for more.",
        "insufficient_quota", "budget_exhausted",
    )


def settle(item: dict, model: str, amount: int, tokens_in: int, tokens_out: int,
           *, estimated: bool = False, failed: bool = False, refund: bool = False,
           now: float | None = None) -> dict:
    """Correct the reserve to the real cost and write the day-and-model usage row.

    `failed`: the request did not get a reply. With `refund` (the upstream never received it, or refused it with
    a 4xx) it costs nothing. Without `refund` (sent, then a timeout or a 5xx) it costs the whole reserve, and the
    tokens passed in are the reserve's bounds, counted as estimated.

    Returns the key item after the update (ALL_NEW).
    """
    if failed and refund:
        actual = 0
        tokens_in = tokens_out = 0
    elif failed:
        actual = amount
        estimated = True
    else:
        actual = pricing.cost(model, tokens_in, tokens_out)
    delta = actual - amount
    new = aws.keys_table().update_item(
        Key={"key_hash": item["key_hash"]},
        UpdateExpression="ADD spent_micro :delta, tokens_in :in, tokens_out :out",
        ExpressionAttributeValues={":delta": delta, ":in": tokens_in, ":out": tokens_out},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    adds = {"requests": 1, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_micro": actual}
    if failed:
        adds["requests_failed"] = 1
    if estimated:
        adds["requests_estimated"] = 1
    names = {f"#a{i}": k for i, k in enumerate(adds)}
    aws.usage_table().update_item(
        Key={"key_id": item["key_id"], "sk": f"{utc_day(now)}#{model}"},
        UpdateExpression="ADD " + ", ".join(f"#a{i} :v{i}" for i in range(len(adds))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(adds.values())},
    )
    return new
