"""Settings, all from the environment. Nothing secret is written in code or in the repo (README, "Decisions": keys).

COURSE_GATEWAY_URL base URL of the course LLM gateway, ending in /v1 (the older GATEWAY_URL still works)
KB_MCP_TOKEN       your course MCP token (the older COURSE_MCP_TOKEN still works); the gateway takes it as the API key and charges your budget
PREFIX             first part of every AWS resource name (default "recipe")
AWS_REGION         default ca-central-1
CHAT_MODEL         default qwen3.6-35b-a3b, the cheaper chat model on the course LLM gateway
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ca-central-1"))
    # COURSE_GATEWAY_URL and KB_MCP_TOKEN; the older names GATEWAY_URL and COURSE_MCP_TOKEN still work when these are not set
    gateway_url: str = field(default_factory=lambda: (os.environ.get("COURSE_GATEWAY_URL")
                                                      or os.environ.get("GATEWAY_URL", "")).rstrip("/"))
    token: str = field(default_factory=lambda: os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN", ""),
                       repr=False)
    chat_model: str = field(default_factory=lambda: os.environ.get("CHAT_MODEL", "qwen3.6-35b-a3b"))

    @property
    def traces_table(self) -> str:
        return f"{self.prefix}-chat-traces"

    @property
    def cache_table(self) -> str:
        return f"{self.prefix}-chat-cache"

    @property
    def spans_table(self) -> str:
        return f"{self.prefix}-chat-spans"

    @property
    def alarm_prefix(self) -> str:
        return f"{self.prefix}-chat-"

    def alarm_name(self, slug: str) -> str:
        """The name of one alarm this recipe creates (ext-trace-alarms/alarms.py: the prefix and the slug)."""
        return f"{self.alarm_prefix}{slug}"

    @property
    def topic_name(self) -> str:
        return f"{self.prefix}-chat-alarms"

    @property
    def metric_namespace(self) -> str:
        return f"{self.prefix}/dashboard-chat"
