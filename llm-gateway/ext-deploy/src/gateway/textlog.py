"""Optional log of what was asked and what came back: one item per forwarded request.

Off unless TABLE_TEXTS names a table. The item's key is the key id and the request id, the two values on every
log line, so a line leads to its text with one GetItem. A failed write is logged and changes nothing in the reply.
The text is the callers' own words: it goes to the table and never to a log line.
"""

import json
import logging
import time
from datetime import UTC, datetime

from . import aws, settings

log = logging.getLogger("gateway")
REPLY_FIELDS = ("content", "reasoning_content", "reasoning", "tool_calls", "refusal")


def reply_from_body(data) -> dict | None:
    """The first choice of a chat reply: its message fields and the finish reason. None for anything else."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    reply = {k: message[k] for k in REPLY_FIELDS if message.get(k)}
    reply["finish_reason"] = choices[0].get("finish_reason")
    return reply


def _fit(request: str, reply: str) -> tuple[str, int]:
    """Cut the middle out of a request that would take the item over the size limit. Returns it and its full size."""
    size = len(request.encode())
    room = settings.MAX_TEXT_ITEM_BYTES - len(reply.encode())
    if size <= room:
        return request, 0
    half = max(room, 2_000) // 2
    raw = request.encode()
    return raw[:half].decode(errors="ignore") + "\n...\n" + raw[-half:].decode(errors="ignore"), size


def write(rid: str, item: dict, model: str, endpoint: str, status: int, body: dict | None, reply: dict | None) -> None:
    s = settings.load()
    if not s.table_texts or body is None:
        return
    try:
        now = int(time.time())
        reply_text = json.dumps(reply, ensure_ascii=False) if reply else ""
        request_text, full_size = _fit(json.dumps(body, ensure_ascii=False), reply_text)
        attrs = {"request_id": rid, "team": str(item.get("team") or "-"), "source": "gateway", "endpoint": endpoint,
                 "model": model, "status": status, "ts": datetime.fromtimestamp(now, UTC).isoformat(),
                 "request": request_text}
        if reply_text:
            attrs["reply"] = reply_text
        if full_size:
            attrs["truncated"] = full_size  # bytes of the whole request; the item holds its start and its end
        if s.text_ttl_days > 0:
            attrs["ttl"] = now + s.text_ttl_days * 86_400
        # UpdateItem with a SET of every attribute, so the function's role needs no action beyond the ones it has
        aws.texts_table().update_item(
            Key={"key_id": item["key_id"], "sk": rid},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
            ExpressionAttributeNames={f"#{k}": k for k in attrs},
            ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()},
        )
    except Exception as e:  # the text log never breaks or delays a reply beyond its own write
        log.error("text log failed request_id=%s key_id=%s error=%s", rid, item.get("key_id", "-"), type(e).__name__)
