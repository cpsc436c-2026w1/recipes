"""Chat history kept per conversation, each conversation owned by one signed-in user.

Two tables (main/tables.py):
- {PREFIX}-chat-conversations: one item per conversation: conversation_id, owner (the signed-in user's id),
  created_at, last_at, expires_at.
- {PREFIX}-chat-history: one item per turn: conversation_id (partition key), at (sort key: UTC time with
  milliseconds plus a short random suffix), role ("user" or "assistant"), text, expires_at.

Every read and every write checks the owner first. The user id comes from your sign-in layer (for example the
`sub` claim of a verified Cognito token), never from the request body (README, "Decisions": who may read a
conversation).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key

from config import HISTORY_MAX_CHARS, HISTORY_TTL_DAYS, Settings


class NotYourConversation(PermissionError):
    """The conversation does not exist, has expired, or belongs to another user.

    One error for all three, so a caller cannot learn whether someone else's conversation id exists.
    """


def _at(t: float) -> str:
    """Sort key for a turn: sorts by time as text; the suffix keeps two turns in the same millisecond apart."""
    stamp = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return f"{stamp}#{uuid.uuid4().hex[:6]}"


class ChatHistory:
    def __init__(self, s: Settings, resource, clock=time.time, ttl_days: int = HISTORY_TTL_DAYS):
        self.s = s
        self.conversations = resource.Table(s.conversations_table)
        self.turns = resource.Table(s.history_table)
        self.client = resource.meta.client
        self.clock = clock
        self.ttl_s = ttl_days * 86400

    def start(self, user_id: str) -> str:
        """Open a conversation for this user and return its id (random, so not guessable; still checked)."""
        now = self.clock()
        cid = uuid.uuid4().hex
        self.conversations.put_item(
            Item={"conversation_id": cid, "owner": user_id, "created_at": Decimal(int(now)),
                  "last_at": Decimal(int(now)), "expires_at": Decimal(int(now + self.ttl_s))},
            ConditionExpression="attribute_not_exists(conversation_id)",
        )
        return cid

    def _check_owner(self, user_id: str, conversation_id: str) -> None:
        # A strongly consistent read: a conversation started a moment ago is found (L12, read consistency).
        item = self.conversations.get_item(Key={"conversation_id": conversation_id},
                                           ConsistentRead=True).get("Item")
        # README, "Decisions": who may read a conversation. Missing, expired and foreign look the same.
        if not item or item["owner"] != user_id or int(item["expires_at"]) <= self.clock():
            raise NotYourConversation(conversation_id)

    def append(self, user_id: str, conversation_id: str, role: str, text: str) -> str:
        """Add one turn. The owner check and the write are one transaction: both happen or neither does."""
        now = self.clock()
        at = _at(now)
        expires = Decimal(int(now + self.ttl_s))
        try:
            # README, "Decisions": owner check on writes. A ConditionCheck cannot sit beside an Update of the
            # same item, so the Update carries the condition and moves the conversation's expiry forward.
            self.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self.s.conversations_table,
                    "Key": {"conversation_id": conversation_id},
                    "UpdateExpression": "SET last_at = :now, expires_at = :exp",
                    "ConditionExpression": "#o = :u AND expires_at > :now",
                    "ExpressionAttributeNames": {"#o": "owner"},
                    "ExpressionAttributeValues": {":u": user_id, ":now": Decimal(int(now)), ":exp": expires},
                }},
                {"Put": {
                    "TableName": self.s.history_table,
                    "Item": {"conversation_id": conversation_id, "at": at, "role": role, "text": text,
                             "expires_at": expires},
                    "ConditionExpression": "attribute_not_exists(#a)",
                    "ExpressionAttributeNames": {"#a": "at"},
                }},
            ])
        except self.client.exceptions.TransactionCanceledException as e:
            # One reason per item of the transaction, in order. Only a failed condition on the conversation
            # (first item) means "not yours". A conflict with another write or throttling is a failure of the
            # write, and the caller must see it as one.
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise NotYourConversation(conversation_id) from e
            raise
        return at

    def recent(self, user_id: str, conversation_id: str, n: int = 10,
               max_chars: int = HISTORY_MAX_CHARS) -> list[dict]:
        """The newest turns that have not expired, newest first: at most n, and at most max_chars of text together,
        but always the newest one. Checks the owner before reading.

        The character cap bounds what the history adds to every later model call (README, "Decisions", row
        "Memory sent to the model"; Agentic AI Lens AGENTCOST07-BP01, memory growth guardrails)."""
        self._check_owner(user_id, conversation_id)
        now = Decimal(int(self.clock()))
        out: list[dict] = []
        kwargs = {
            "KeyConditionExpression": Key("conversation_id").eq(conversation_id),
            "ScanIndexForward": False,  # newest first: the sort key is the time
            # README, "Decisions": expiry by TTL. DynamoDB deletes expired items days later, so filter here.
            "FilterExpression": Attr("expires_at").gt(now),
            # Limit counts items read before the filter, so a page can come back short; keep paging until n.
            "Limit": n,
        }
        while len(out) < n:
            page = self.turns.query(**kwargs)
            out += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        kept, chars = [], 0
        for i in out[:n]:
            if kept and chars + len(i["text"]) > max_chars:
                break
            kept.append({"at": i["at"], "role": i["role"], "text": i["text"]})
            chars += len(i["text"])
        return kept
