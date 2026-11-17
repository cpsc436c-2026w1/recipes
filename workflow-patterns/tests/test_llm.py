"""The gateway client against a local stand-in (httpx.MockTransport), the settings, and the files as a student runs them."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from llm import Model, Settings

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "LLM_API_KEY", "COURSE_MCP_TOKEN", "LLM_MODEL")


@pytest.fixture(autouse=True)
def no_settings_from_this_machine(monkeypatch, tmp_path):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)                       # no .env file here


def gateway(replies: list, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = replies[len(seen) - 1]
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def settings(**env) -> Settings:
    return Settings(**{"COURSE_GATEWAY_URL": "https://gateway.example/v1", "KB_MCP_TOKEN": "test-token", **env})


def test_gateway_call_has_the_openai_chat_shape_and_the_token_as_bearer_key():
    seen = []
    ok = (200, {"choices": [{"message": {"content": " oat milk \n"}}]})
    model = Model(settings=settings(), transport=gateway([ok], seen))
    assert model.chat("the system prompt", "the user message", 0.7) == "oat milk"
    request = seen[0]
    assert str(request.url) == "https://gateway.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-token"
    body = json.loads(request.content)
    assert body == {"model": "qwen3.8-27b", "max_tokens": 1500, "temperature": 0.7,
                    "messages": [{"role": "system", "content": "the system prompt"},
                                 {"role": "user", "content": "the user message"}]}


@pytest.mark.parametrize("reply, message", [
    ((429, {"error": {"code": "budget_exhausted"}}), "refused the call: HTTP 429"),
    ((200, {"error": "upstream"}), "not a chat completion"),
    ((200, {"choices": [{"message": {"content": None}}]}), "token cap"),
])
def test_a_failed_gateway_call_is_an_error_with_its_reason_and_is_not_retried(reply, message):
    seen = []
    model = Model(settings=settings(), transport=gateway([reply], seen))
    with pytest.raises(RuntimeError, match=message):
        model.chat("s", "u")
    assert len(seen) == 1


def test_settings_read_the_course_names_then_the_fallbacks(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "from-llm-api-key")
    monkeypatch.setenv("GATEWAY_URL", "https://old.example/v1")
    s = Settings()
    assert s.token.get_secret_value() == "from-llm-api-key" and s.gateway_url == "https://old.example/v1"
    assert s.model == "qwen3.8-27b" and "from-llm-api-key" not in repr(s)
    monkeypatch.setenv("KB_MCP_TOKEN", "from-kb-mcp-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://new.example/v1")
    monkeypatch.setenv("LLM_MODEL", "another-model")
    s = Settings()
    assert (s.token.get_secret_value(), s.gateway_url, s.model) == ("from-kb-mcp-token", "https://new.example/v1", "another-model")


def test_env_example_names_the_settings_and_holds_no_value_that_could_be_a_key():
    lines = dict(l.split("=", 1) for l in (ROOT / ".env.example").read_text().splitlines() if "=" in l and not l.startswith("#"))
    assert set(lines) == {"COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LLM_MODEL"}
    assert lines["LLM_MODEL"] == Settings.model_fields["model"].default
    assert lines["KB_MCP_TOKEN"].startswith("<") and lines["COURSE_GATEWAY_URL"].startswith("<")


def run_file(name: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in NAMES}
    return subprocess.run([sys.executable, str(ROOT / "main" / name), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name, calls", [("chain.py", 3), ("parallel.py", 5), ("evaluator.py", 6)])
def test_each_file_runs_offline_and_prints_what_the_captured_stub_run_has(name, calls, tmp_path):
    done = run_file(name, cwd=tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines()[0].startswith("model: stub") and done.stdout.splitlines()[-1] == f"model calls: {calls}"
    steady = lambda text: [line for line in text.splitlines() if not line.startswith("  seconds:")]   # timing can differ
    assert steady(done.stdout) == steady((ROOT / "output" / f"{name[:-3]}-stub.txt").read_text())


@pytest.mark.parametrize("name", ["chain.py", "parallel.py", "evaluator.py"])
def test_a_low_cap_stops_the_run_cleanly_and_a_gateway_run_without_settings_makes_no_call(name, tmp_path):
    capped = run_file(name, "--max-calls", "1", cwd=tmp_path)
    assert capped.returncode == 0 and "stopped: call cap reached: 1 model calls" in capped.stdout
    assert capped.stdout.splitlines()[-1] == "model calls: 1"
    live = run_file(name, "--model", "gateway", cwd=tmp_path)
    assert live.returncode != 0 and "set COURSE_GATEWAY_URL and KB_MCP_TOKEN" in live.stderr and "Traceback" not in live.stderr


def test_each_pattern_file_is_short_and_recipe_toml_names_files_that_exist():
    for name in ("chain.py", "parallel.py", "evaluator.py"):
        assert len((ROOT / "main" / name).read_text().splitlines()) <= 80, name
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name and r["region"] == "ca-central-1" and r["constraints"]["no_keys_in_code"] is True
    assert all((ROOT / f).exists() for f in r["instruments"]["files"])
    assert r["commands"]["setup"] == "uv sync" and r["commands"]["test"] == "uv run pytest -q"
    assert r["commands"]["run"].startswith("uv run python")
