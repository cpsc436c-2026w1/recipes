"""Budget notifications at 50, 80 and 100 percent, once per threshold per key.

`notified` is a string set. ADD creates it when missing. The conditional update
with NOT contains lets exactly one of two racing requests win, and only the
winner publishes.
"""

import logging

from botocore.exceptions import ClientError

from . import aws, pricing, settings
from .budget import utc_day

log = logging.getLogger("gateway")


def check_thresholds(item: dict) -> list[int]:
    """Publish each newly crossed threshold. Returns the thresholds published."""
    budget = item.get("budget_micro")
    if not budget:
        return []  # admin keys have no budget and never notify
    pct = int(item.get("spent_micro", 0)) * 100 // int(budget)
    already = set(item.get("notified", set()))
    sent = []
    for t in settings.THRESHOLDS:
        if pct < t or str(t) in already:
            continue
        try:
            aws.keys_table().update_item(
                Key={"key_hash": item["key_hash"]},
                UpdateExpression="ADD notified :tset",
                ConditionExpression="NOT contains(notified, :t)",
                ExpressionAttributeValues={":tset": {str(t)}, ":t": str(t)},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise
        _publish(item, t)
        sent.append(t)
    return sent


def notify_refused(item: dict) -> bool:
    """Publish the 100 % notice when a request is refused on budget, once per key.

    A key is refused when the request's worst case no longer fits, which usually happens before spent reaches
    100 %, so check_thresholds alone would never send the 100 % notice. This one shares the "100" marker with
    it, so a key gets one 100 % notice whichever path comes first. Errors are logged, never raised.
    """
    if not item.get("budget_micro"):
        return False
    try:
        cur = aws.keys_table().update_item(
            Key={"key_hash": item["key_hash"]},
            UpdateExpression="ADD notified :tset",
            ConditionExpression="NOT contains(notified, :t)",
            ExpressionAttributeValues={":tset": {"100"}, ":t": "100"},
            ReturnValues="ALL_NEW",
        )["Attributes"]
        _publish(cur, 100, refused=True)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            log.error("refusal notice failed key_id=%s error=%s", item["key_id"], e.response["Error"]["Code"])
    except Exception as e:  # never let a notice break the 429
        log.error("refusal notice failed key_id=%s error=%s", item["key_id"], type(e).__name__)
    return False


def _publish(item: dict, t: int, refused: bool = False) -> None:
    topic = settings.load().topic_arn
    if not topic:
        log.warning("no TOPIC_ARN set, threshold %s for key %s not published", t, item["key_id"])
        return
    owner = str(item.get("owner", ""))
    kind = str(item.get("kind", ""))
    body = (
        (f"Key {item['key_id']} ({kind}, owner {owner}) refused a request: its worst-case cost no longer fits "
         "in what is left of the budget.\n" if refused else
         f"Key {item['key_id']} ({kind}, owner {owner}) has used {t}% of its budget.\n")
        + f"Spent {pricing.micro_to_amount(int(item.get('spent_micro', 0)))} {settings.CURRENCY} "
        f"of {pricing.micro_to_amount(int(item['budget_micro']))} {settings.CURRENCY}.\n"
        f"Tokens in {int(item.get('tokens_in', 0))}, tokens out {int(item.get('tokens_out', 0))}.\n"
        f"Day {utc_day()} (UTC).\n"
    )
    aws.sns().publish(
        TopicArn=topic,
        Subject=(f"llm gateway: {owner} budget used up (request refused)" if refused
                 else f"llm gateway: {owner} at {t}% of budget")[:100],
        Message=body,
        MessageAttributes={
            "owner": {"DataType": "String", "StringValue": owner or "-"},
            "kind": {"DataType": "String", "StringValue": kind or "-"},
        },
    )
    log.info("notify key_id=%s threshold=%s", item["key_id"], t)
