"""Settings, all from the environment; nothing secret lives in code (README, "Decisions").

PREFIX      first part of every AWS resource name (default "recipe")
AWS_REGION  default ca-central-1
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# How long each kind of item lives before DynamoDB may delete it (README, "Decisions": expiry by TTL).
# All three are ours (assumed), sized for a course project; change them for your users.
HISTORY_TTL_DAYS = 30
# The most characters of history that recent() returns for one model call (ours); about 1,000 tokens of English
# if a token is about 4 characters (assumed).
HISTORY_MAX_CHARS = 4000
CACHE_TTL_HOURS = 24
PERSONAL_CACHE_TTL_HOURS = 1   # an answer stored for one user (an order's status) goes stale sooner
TRACE_TTL_DAYS = 7


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ca-central-1"))

    @property
    def conversations_table(self) -> str:
        return f"{self.prefix}-chat-conversations"

    @property
    def history_table(self) -> str:
        return f"{self.prefix}-chat-history"

    @property
    def cache_table(self) -> str:
        return f"{self.prefix}-answer-cache"

    @property
    def traces_table(self) -> str:
        return f"{self.prefix}-llm-traces"

    def all_tables(self) -> list[str]:
        return [self.conversations_table, self.history_table, self.cache_table, self.traces_table]
