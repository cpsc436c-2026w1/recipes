"""Settings, all from the environment. The token is never written in code or in the repo (README, "Decisions": token).

MCP_SERVER_TOKEN   the bearer token a client must send; the server refuses to start without it
MCP_HOST           default 127.0.0.1 (this laptop only)
MCP_PORT           default 8765
DATA_DIR           default data/ in this folder: the Parquet files the server reads
MAX_ROWS           default 50: the most rows one query returns to the model
MCP_ALLOWED_ORIGINS  only when MCP_HOST is not this machine: the sites the server answers, comma-separated, for
                   example https://team.example.org
QUERY_TIMEOUT_S    default 5: the longest one query may run, in seconds
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from main.engine import QUERY_TIMEOUT_S

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


@dataclass(frozen=True)
class Settings:
    token: str = field(default_factory=lambda: os.environ.get("MCP_SERVER_TOKEN", ""), repr=False)
    host: str = field(default_factory=lambda: os.environ.get("MCP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("MCP_PORT", "8765")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", ROOT / "data")))
    max_rows: int = field(default_factory=lambda: int(os.environ.get("MAX_ROWS", "50")))
    allowed_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip().rstrip("/") for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()))
    query_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("QUERY_TIMEOUT_S", QUERY_TIMEOUT_S)))

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"
