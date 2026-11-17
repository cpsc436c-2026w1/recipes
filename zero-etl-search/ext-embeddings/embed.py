"""Embedders: a deterministic stand-in for tests, and the course LLM gateway for a real run.

The gateway takes your course MCP token as the API key and charges your budget; provider keys never reach you.
COURSE_GATEWAY_URL base URL of the course LLM gateway, ending in /v1 (the older GATEWAY_URL still works)
KB_MCP_TOKEN       your course MCP token, read from the environment only (the older COURSE_MCP_TOKEN still works)
"""

from __future__ import annotations

import hashlib
import math
import os
import re

EMBEDDING_MODEL = "qwen3-embedding-0.6b"
EMBEDDING_DIM = 1024  # measured: recipes/llm-probes/output.txt, 2026-11-10


class StubEmbedder:
    """Hashed words into a small vector, normalised. Shows the flow, not how good embeddings are."""

    model = "stub-hash-64"
    dim = 64

    def __init__(self, fail_for: set[str] | None = None):
        self.fail_for = fail_for or set()  # tests: texts that make the "model" fail, to reach the DLQ
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            self.calls += 1
            if any(f in t for f in self.fail_for):
                raise RuntimeError("embedding call failed")
            v = [0.0] * self.dim
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class GatewayEmbedder:
    model = EMBEDDING_MODEL
    dim = EMBEDDING_DIM

    def __init__(self, url: str | None = None, token: str | None = None):
        env = os.environ
        self.url = (url or env.get("COURSE_GATEWAY_URL") or env["GATEWAY_URL"]).rstrip("/")
        self.token = token or env.get("KB_MCP_TOKEN") or env["COURSE_MCP_TOKEN"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        r = httpx.post(f"{self.url}/embeddings", headers={"Authorization": f"Bearer {self.token}"},
                       json={"model": self.model, "input": texts}, timeout=60)
        r.raise_for_status()  # a 429 (budget or rate) fails the message; SQS retries it later
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
