"""Settings, all from the environment. Nothing secret is written in code or in the repo.

COURSE_GATEWAY_URL base URL of the course LLM gateway, ending in /v1 (the older GATEWAY_URL still works)
KB_MCP_TOKEN       your course MCP token (the older COURSE_MCP_TOKEN still works); the gateway takes it as the API key and charges your budget
PREFIX             first part of every AWS resource name (default "recipe")
AWS_REGION         default ca-central-1
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

CHEAP_MODEL = "qwen3.6-35b-a3b"
STRONG_MODEL = "qwen3.8-27b"
EMBEDDING_MODEL = "qwen3-embedding-0.6b"
EMBEDDING_DIM = 1024  # measured: llm-probes/output/all-probes-2026-11-11.txt

# Decision (reply cap per model): an answer here is one or two sentences that cite ids, and 150 tokens hold that for
# the cheaper model. The stronger model reasons before it answers, and the reasoning counts against max_tokens: with
# 150 its reply came back empty (finish_reason "length") in the gateway run of 2026-11-14
# (output/routing-gateway-150-cap.txt). 600 is the cap the model-routing recipe gives the same model
# (model-routing/main/paths.py, HARD_MAX_TOKENS). A model this table does not name gets the larger cap.
ANSWER_MAX_TOKENS = {CHEAP_MODEL: 150, STRONG_MODEL: 600}


def answer_max_tokens(model: str) -> int:
    return ANSWER_MAX_TOKENS.get(model, max(ANSWER_MAX_TOKENS.values()))


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ca-central-1"))
    # COURSE_GATEWAY_URL and KB_MCP_TOKEN; the older names GATEWAY_URL and COURSE_MCP_TOKEN still work when these are not set
    gateway_url: str = field(default_factory=lambda: (os.environ.get("COURSE_GATEWAY_URL")
                                                      or os.environ.get("GATEWAY_URL", "")).rstrip("/"))
    token: str = field(default_factory=lambda: os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN", ""),
                       repr=False)

    @property
    def collection(self) -> str:
        return f"{self.prefix}-products"  # 3 to 64 characters, a lower-case letter first, then lower-case letters, digits, - and _ (boto3 service model)

    @property
    def index(self) -> str:
        return "products"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_products() -> list[dict]:
    return load_jsonl(DATA_DIR / "products.jsonl")


def load_queries() -> list[dict]:
    return load_jsonl(DATA_DIR / "queries.jsonl")


def doc_text(p: dict) -> str:
    """The text both indexes see for one product: the same text, so the comparison is about the method."""
    return f"{p['name']}. {p['description']} {p['category']} {p['brand']}"
