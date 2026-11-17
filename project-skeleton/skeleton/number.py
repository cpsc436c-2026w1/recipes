"""The one number: updates whose event time falls in the last hour.

"Last hour" is the 60 whole minutes before the minute of `as_of` (README, Decisions: "window"). Whole minutes let the
operational API add up per-minute counters and still agree exactly with the lake queries.

The SQL below runs unchanged on Athena (in AWS) and on DuckDB (the offline stand-in): both read the TIMESTAMP literal
and compare fixed-width ISO strings in the same way.
"""
from datetime import datetime, timedelta, timezone

from skeleton.events import iso


def window(as_of: datetime | None = None) -> tuple[datetime, datetime]:
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = as_of.replace(second=0, microsecond=0)
    return end - timedelta(hours=1), end


def minutes(start: datetime, end: datetime) -> list[str]:
    """Minute keys "YYYY-MM-DDTHH:MM" in [start, end): the counters the operational API adds up."""
    out, t = [], start
    while t < end:
        out.append(t.strftime("%Y-%m-%dT%H:%M"))
        t += timedelta(minutes=1)
    return out


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def lake_sql(start: datetime, end: datetime, table: str = "updates") -> str:
    """Over the Parquet lake written by Firehose (rep-lakehouse-first).

    dt is the partition (the day the update arrived). An update arrives at or after its event time, so
    dt >= the day of `start` keeps every row the window needs and lets Athena skip older days.
    count(DISTINCT event_id): Kinesis and Firehose deliver at least once, so a copy may reach the lake twice.
    """
    return (f"SELECT count(DISTINCT event_id) AS updates_last_hour FROM {table} "
            f"WHERE dt >= '{start:%Y-%m-%d}' "
            f"AND event_time >= TIMESTAMP '{_ts(start)}' AND event_time < TIMESTAMP '{_ts(end)}'")


# rep-genai-first: the model sees this view, plain columns over the export, instead of DynamoDB's typed JSON.
EVENTS_VIEW_SQL = ("SELECT Item.event_id.S AS event_id, Item.entity_id.S AS entity_id, "
                   "Item.event_time.S AS event_time FROM updates_export WHERE Item.kind.S = 'event'")


def export_sql(start: datetime, end: datetime, table: str = "updates_export") -> str:
    """Over a DynamoDB export (rep-operational-first, rep-genai-first): one row per item, typed as in DynamoDB JSON.

    Only items of kind "event" are updates; the table also holds per-key state and per-minute counters.
    The table keeps one item per event_id, so count(*) does not count a copy twice.
    """
    return (f"SELECT count(*) AS updates_last_hour FROM {table} "
            f"WHERE Item.kind.S = 'event' "
            f"AND Item.event_time.S >= '{iso(start)}' AND Item.event_time.S < '{iso(end)}'")
