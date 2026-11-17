"""The environment of an offline run, for pytest (tests/conftest.py) and behave (features/environment.py)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Names a shell on this machine might hold: none of them may reach a test.
CLEARED = ("AWS_PROFILE", "AWS_DEFAULT_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "LLM_BASE_URL", "LLM_API_KEY",
           "OPENAI_BASE_URL", "OPENAI_API_KEY", "TOOLS_GATEWAY_URL", "KB_MCP_TOKEN_PARAM", "LOGFIRE_TOKEN_PARAM",
           "BOOKS_TABLE", "HOLDS_TABLE")
OVERRIDES = {"PREFIX": "test", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
             "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "ca-central-1",
             "AWS_EC2_METADATA_DISABLED": "true",
             "COURSE_GATEWAY_URL": "http://127.0.0.1:9",       # a stray model call cannot reach the course LLM gateway
             "LOGFIRE_SEND_TO_LOGFIRE": "false"}


def example_lines(path: Path = ROOT / ".env.example") -> dict[str, str]:
    """NAME -> value for every setting line of .env.example (comment lines left out)."""
    found = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.partition("=")
            found[name] = value
    return found


def example_env(env=os.environ) -> None:
    """Every line of .env.example, empty values as empty strings, then the test overrides. A variable of this
    machine's shell cannot win."""
    for name in (*CLEARED, *example_lines()):
        env.pop(name, None)
    env.update(example_lines())
    env.update(OVERRIDES)


def no_wait(seconds: float) -> None:
    """In place of time.sleep in the deploy and clean-up loops."""
