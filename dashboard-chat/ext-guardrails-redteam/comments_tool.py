"""The one way rider text reaches the model: the tool get_comments, whose SQL the code writes (README, "Injection
through stored text").

The model chooses a zone and two times; the query is fixed and its values are bound parameters, so the model cannot
choose the columns, and every comment comes back with its id and zone. Each text goes inside a data block,
<data id="c-0012" zone="161">...</data>, with < and > written as &lt; and &gt;, so a comment cannot close its block or
open a new one. The count is computed here, by the code, so a number in the reply can be checked against it.
"""

from __future__ import annotations

from datetime import datetime

from main.engine import MAX_CELL_CHARS, Engine

MAX_COMMENTS = 50  # rows of one call (ours: the same limit as run_sql's rows)
COMMENTS_SQL = ("SELECT comment_id, pickup_zone_id, comment_time, text FROM rider_comments "
                "WHERE pickup_zone_id = ? AND comment_time >= ? AND comment_time < ? "
                f"ORDER BY comment_time LIMIT {MAX_COMMENTS}")

GET_COMMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_comments",
        "description": ("Return the rider comments of one pickup zone between two times (at most 50), each inside a "
                        "data block with its comment_id, and their count."),
        "parameters": {
            "type": "object",
            "properties": {
                "zone": {"type": "integer", "description": "pickup zone id, for example 161"},
                "start": {"type": "string", "description": "first time, 'YYYY-MM-DD HH:MM' (UTC), included"},
                "end": {"type": "string", "description": "last time, 'YYYY-MM-DD HH:MM' (UTC), not included"},
            },
            "required": ["zone", "start", "end"],
        },
    },
}


class BadArguments(ValueError):
    pass


def escape(text: str) -> str:
    """< and > as &lt; and &gt;: a comment cannot write a tag of its own."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def data_block(comment_id: str, zone: int, text: str) -> str:
    return f'<data id="{comment_id}" zone="{zone}">{escape(text)}</data>'


def parse_time(value) -> datetime:
    text = str(value).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise BadArguments(f"the time {str(value)[:40]!r} is not 'YYYY-MM-DD HH:MM'")


def parse_arguments(arguments: dict) -> tuple[int, datetime, datetime]:
    try:
        zone = int(str(arguments.get("zone", "")).strip())
    except ValueError:
        raise BadArguments("zone must be a zone id, for example 161") from None
    start, end = parse_time(arguments.get("start", "")), parse_time(arguments.get("end", ""))
    if end <= start:
        raise BadArguments("end must be after start")
    return zone, start, end


def get_comments(engine: Engine, arguments: dict) -> dict:
    """The tool result: {"count": n, "comments": [data block, ...], "truncated": ...}, plus the rows the checks keep
    under "_rows" (comment_id, zone); the caller removes "_rows" before the result goes to the model."""
    zone, start, end = parse_arguments(arguments)
    cur = engine.con.cursor()
    cur.execute("BEGIN TRANSACTION READ ONLY")
    try:
        rows = cur.execute(COMMENTS_SQL, [zone, start, end]).fetchall()
    finally:
        cur.execute("ROLLBACK")
    cut = any(len(text) > MAX_CELL_CHARS for _, _, _, text in rows)
    blocks = [data_block(cid, z, text[:MAX_CELL_CHARS]) for cid, z, _, text in rows]
    return {"count": len(rows), "comments": blocks, "truncated": "text" if cut else None,
            "_rows": [(cid, int(z)) for cid, z, _, _ in rows]}
