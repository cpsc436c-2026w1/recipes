import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_script(rel: str):
    """Import an extension script by path (its folder name has a hyphen)."""
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def no_gateway_env(monkeypatch):
    # Offline tests never reach a real endpoint, even on a machine where the variables are set.
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("COURSE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("COURSE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
