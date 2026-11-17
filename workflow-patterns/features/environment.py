"""behave set-up. Run from the recipe folder: `uv run behave`. Every scenario runs on a stub: no key, no network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "main"))
NOT_SET = ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "LLM_API_KEY", "COURSE_MCP_TOKEN", "LLM_MODEL")


def before_scenario(context, scenario):
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.model = None
    context.out = None
    context.error = None
