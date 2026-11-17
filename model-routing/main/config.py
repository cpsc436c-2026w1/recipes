"""Settings, all from the environment. Nothing secret is written in code or in the repo (README, "Decisions").

COURSE_GATEWAY_URL base URL of the course LLM gateway, ending in /v1 (the older GATEWAY_URL still works)
KB_MCP_TOKEN       your course MCP token (the older COURSE_MCP_TOKEN still works); the gateway takes it as the API key and charges your budget
SMALL_MODEL        default qwen3.6-35b-a3b, the cheaper chat model on the course LLM gateway: the router and the
                   search path
LARGE_MODEL        default qwen3.8-27b, the stronger chat model: the hard path only
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


@dataclass(frozen=True)
class Settings:
    # COURSE_GATEWAY_URL and KB_MCP_TOKEN; the older names GATEWAY_URL and COURSE_MCP_TOKEN still work when these are not set
    gateway_url: str = field(default_factory=lambda: (os.environ.get("COURSE_GATEWAY_URL")
                                                      or os.environ.get("GATEWAY_URL", "")).rstrip("/"))
    token: str = field(default_factory=lambda: os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN", ""),
                       repr=False)
    small_model: str = field(default_factory=lambda: os.environ.get("SMALL_MODEL", "qwen3.6-35b-a3b"))
    large_model: str = field(default_factory=lambda: os.environ.get("LARGE_MODEL", "qwen3.8-27b"))
