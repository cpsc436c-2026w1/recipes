"""Embeddings: the course LLM gateway's embedding model for real runs, a hashed stand-in for tests.

GatewayEmbedder sends POST {COURSE_GATEWAY_URL}/embeddings with model qwen3-embedding-0.6b (1,024 numbers per text).

StubEmbedder is not a language model. It hashes words and their three-letter pieces into 512 numbers, after
mapping a few words to a shared concept (SYNONYMS). The pieces make a typo land near the right word ("speeker" shares
"spe", "eke"... with "speaker"); the concept map stands in for what a real embedding model learns from text
("couch" near "sofa"). It is there so the tests are deterministic and offline. Its recall numbers show how the eval
works, not how good embedding search is: for that, run the eval with --embedder gateway (README, "Run it").
"""

from __future__ import annotations

import hashlib
import time

import httpx
import numpy as np

from main.config import EMBEDDING_MODEL
from main.llm import GatewayRefused, post
from main.text import STOP

STUB_DIM = 96  # small on purpose: unrelated words share slots, so the stub makes mistakes as real search does

SYNONYMS = {
    "couch": "sofa", "sofa": "sofa", "settee": "sofa",
    "apartment": "studio", "studio": "studio", "flat": "studio",
    "laptop": "notebook", "notebook": "notebook",
    "earphones": "earbuds", "earphone": "earbuds", "earbuds": "earbuds", "earbud": "earbuds", "buds": "earbuds",
    "battery": "battery", "power": "battery", "bank": "battery", "external": "portable", "portable": "portable",
    "back": "lumbar", "lumbar": "lumbar", "pain": "support", "support": "support",
    "calls": "microphone", "call": "microphone", "microphone": "microphone",
    "camping": "backpacking", "backpacking": "backpacking",
    "maker": "machine", "machine": "machine",
}


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little")


class StubEmbedder:
    dim = STUB_DIM

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in _words(text):
                concept = SYNONYMS.get(word, word)
                out[row, _h("w:" + concept) % self.dim] += 1.0
                padded = f"#{word}#"
                for i in range(len(padded) - 2):
                    out[row, _h("g:" + padded[i:i + 3]) % self.dim] += 0.3
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


def _words(text: str) -> list[str]:
    import re

    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]


class GatewayEmbedder:
    """Needs COURSE_GATEWAY_URL and KB_MCP_TOKEN (or the older GATEWAY_URL and COURSE_MCP_TOKEN). Returns unit vectors.

    Decision (gateway refusals): texts go out in batches of `batch` (32, ours), and a text is sent once: its vector
    is kept, so the three passes of the eval over the same queries (embedding, hybrid, answers) cost one request,
    not three per query. main/run.py embeds all queries in one call before the eval. A rate-limit refusal is waited
    out by main.llm.post; a spent budget or a bad key stops the run.
    """

    dim = 1024

    def __init__(self, base_url: str, token: str, model: str = EMBEDDING_MODEL, batch: int = 32,
                 http: httpx.Client | None = None, sleep=time.sleep):
        if not base_url or not token:
            raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, 'Run it')")
        self.model, self.batch, self.sleep = model, batch, sleep
        self.http = http or httpx.Client(base_url=base_url, timeout=60,
                                         headers={"Authorization": f"Bearer {token}"})
        self.tokens_in = 0
        self.requests = 0
        self._kept: dict[str, np.ndarray] = {}

    def embed(self, texts: list[str]) -> np.ndarray:
        new = [t for t in dict.fromkeys(texts) if t not in self._kept]
        for i in range(0, len(new), self.batch):
            part = new[i:i + self.batch]
            data = post(self.http, "/embeddings", {"model": self.model, "input": part}, self.sleep)
            self.requests += 1
            self.tokens_in += (data.get("usage") or {}).get("prompt_tokens", 0)
            if not isinstance(data.get("data"), list):
                raise GatewayRefused(200, "not_an_embedding_list", f"no data in {str(data)[:200]!r}")
            rows = sorted(data["data"], key=lambda d: d["index"])
            if len(rows) != len(part):
                raise RuntimeError(f"the gateway returned {len(rows)} embeddings for {len(part)} texts")
            for text, row in zip(part, rows):
                v = np.array(row["embedding"], dtype=np.float32)
                self._kept[text] = v / (np.linalg.norm(v) or 1.0)
        return np.array([self._kept[t] for t in texts], dtype=np.float32)
