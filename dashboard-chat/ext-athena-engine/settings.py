"""Names and limits of ext-athena-engine. Names follow PREFIX (README, "Resources"); limits are the README's
Decisions rows. Nothing secret is here.

PREFIX          first part of every resource name (default "recipe"); Glue names use it with "-" as "_"
CHAT_DATABASE   the Glue database the model's tables are in (default <prefix>_chat); deploy.py builds only that one
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

REGION = "ca-central-1"
MB = 2**20  # 1 MB = 2^20 bytes, 1 GB = 2^30, 1 TB = 2^40 (the course price sheet's units)
TB = 2**40

# Decision (limits outside the model): the question's deadline and each wait's share of it.
DEADLINE_S = 18.0  # the question ends at 18 s; the SLO is 20 s at p95 (R130, R210)
MODEL_CALL_MAX_S = 6.0  # one model call waits at most this, cut to the time left
QUERY_MAX_S = 10.0  # one query waits at most this, cut to the time left
MAX_QUERIES = 3  # queries a question may run, also when one reply asks for more (R160)
POLL_S = 0.5  # GetQueryExecution every half second
GRACE_S = 0.8  # after the deadline, how long the question waits for a stopped query or call to return its numbers

# Decision (scan limits): the workgroup cancels one query past 100 MB; a daily cap in code bounds all questions.
SCAN_CUTOFF_BYTES = 104_857_600  # the workgroup's BytesScannedCutoffPerQuery, set by scripts/lake.py
DAILY_CAP_MB = 5_120  # 5 GB billed a day (R200)
RESERVE_MB = 100  # reserved before each query, settled with what it billed
MIN_BILLED_MB = 10  # Athena bills at least 10 MB a query that ran

# The output-token cap of the chat's model calls through the course gateway: the gateway's own cap. A reasoning
# model spends part of the cap before it answers, and the question's deadline (not the cap) bounds the time.
MODEL_MAX_TOKENS = 16_384

ATHENA_USD_PER_TB = 5.50  # price sheet, ca-central-1, 2026-11-18

# The tool results the model gets for each limit (R150, R160, R200, R210).
SCAN_LIMIT_RESULT = "the query was stopped at the scan limit of 100 MB"
QUERY_LIMIT_RESULT = "query limit reached"
DAILY_LIMIT_RESULT = "daily scan limit reached"

TABLES = ("trips", "zones")
# The columns of the two tables, for the system prompt (README, "Data"). The names are those of the lake table
# <prefix>_lake.trips, so a team's prompt and eval cases carry over.
SCHEMA = {
    "trips": [("trip_id", "varchar"), ("trip_state", "varchar"), ("pickup_zone_id", "integer"),
              ("requested_time", "timestamp"), ("driver_id", "varchar"), ("fare", "double"),
              ("trip_miles", "double"), ("requested_day", "date")],
    "zones": [("zone_id", "integer"), ("borough", "varchar"), ("zone_name", "varchar"),
              ("service_zone", "varchar")],
}


def _prefix() -> str:
    return os.environ.get("PREFIX") or "recipe"


@dataclass(frozen=True)
class Names:
    prefix: str = field(default_factory=_prefix)

    @property
    def glue_prefix(self) -> str:
        return self.prefix.replace("-", "_").lower()

    @property
    def workgroup(self) -> str:
        return f"{self.prefix}-chat-wg"

    @property
    def function(self) -> str:
        return f"{self.prefix}-chat-query"

    @property
    def role(self) -> str:
        return f"{self.prefix}-chat-reader"

    @property
    def usage_table(self) -> str:
        return f"{self.prefix}-chat-usage"

    @property
    def log_group(self) -> str:
        return f"/aws/lambda/{self.function}"

    @property
    def database(self) -> str:
        return os.environ.get("CHAT_DATABASE") or f"{self.glue_prefix}_chat"

    @property
    def data_prefix(self) -> str:
        return "chat/"  # this extension's prefix in the shared lake bucket

    @property
    def results_prefix(self) -> str:
        return "athena-results/chat-wg/"  # the workgroup's result prefix (scripts/lake.py)


@dataclass(frozen=True)
class Limits:
    """The deadline and its shares. Tests shrink them all by one factor to check the wall-clock backstop fast."""
    deadline_s: float = DEADLINE_S
    model_call_max_s: float = MODEL_CALL_MAX_S
    query_max_s: float = QUERY_MAX_S
    max_queries: int = MAX_QUERIES
    poll_s: float = POLL_S
    grace_s: float = GRACE_S

    def scaled(self, factor: float) -> "Limits":
        return Limits(self.deadline_s * factor, self.model_call_max_s * factor, self.query_max_s * factor,
                      self.max_queries, self.poll_s * factor, self.grace_s * factor)


def billed_mb(state: str, scanned_bytes: int, cancelled_at_cutoff: bool = False) -> int:
    """MB Athena bills for one query: Data scanned rounded up to the MB, at least 10 MB, for a query that succeeded
    or was cancelled; 0 for one that failed (Athena pricing: failed queries are not billed, cancelled ones are)."""
    if state == "SUCCEEDED" or state == "CANCELLED" or cancelled_at_cutoff:
        return max(MIN_BILLED_MB, -(-int(scanned_bytes) // MB))
    return 0


def athena_usd(billed_bytes: int) -> float:
    return billed_bytes / TB * ATHENA_USD_PER_TB
