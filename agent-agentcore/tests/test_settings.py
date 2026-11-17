"""The settings object: .env.example is the one list of settings, no default lives in code, a missing one is named."""
import ast
import os
import re
import subprocess
import sys

import pytest
from pydantic import ValidationError

from main.config import ENV_NAMES, SECRETS, Settings, load_settings
from tests.conftest import ROOT, example_lines
from tests.fakes import MODEL


def test_env_example_and_the_settings_class_name_the_same_settings():
    assert set(example_lines()) == set(ENV_NAMES.values())
    assert set(ENV_NAMES) == set(Settings.model_fields)


def test_env_example_has_the_three_sections_in_order_and_a_comment_above_every_setting():
    lines = (ROOT / ".env.example").read_text().splitlines()
    heads = [i for i, line in enumerate(lines) if line.startswith("# ── ")]
    assert [lines[i].split("──")[1].strip()[:2] for i in heads] == ["1.", "2.", "3."]
    for i, line in enumerate(lines):
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'$#]*", line), line     # the subset uv, bash and dotenv share
            assert lines[i - 1].startswith("# ") and "Source:" in lines[i - 1] or "Empty:" in lines[i - 1], line
    secrets = {line.split("=")[0] for line in lines[heads[2]:] if line and not line.startswith("#")}
    assert secrets == {ENV_NAMES[f] for f in SECRETS}
    assert all(example_lines()[name] == "" for name in secrets)                # no secret value is committed


def test_no_setting_has_a_default_in_code():
    assert [name for name, field in Settings.model_fields.items() if not field.is_required()] == []


def test_only_the_settings_file_and_the_deployed_entry_points_read_the_environment():
    readers = set()
    for path in [*ROOT.glob("main/*.py"), *ROOT.glob("evals/*.py"), ROOT / "clean_up.py",
                 *ROOT.glob("tools_lambda/*.py"), *ROOT.glob("runtime/*.py")]:
        tree = ast.parse(path.read_text())
        if any(isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv") for n in ast.walk(tree)):
            readers.add(str(path.relative_to(ROOT)))
    # deployed code reads what the deploy step set; everything else gets the settings object
    assert readers == {"tools_lambda/handler.py", "runtime/agent_entry.py"}


def test_the_settings_object_is_frozen(s):
    with pytest.raises(ValidationError):
        s.prefix = "other"


@pytest.mark.parametrize("name", ["PREFIX", "AGENT_MODEL", "MAX_MODEL_CALLS", "LOGFIRE_ENABLED", "KB_MCP_TOKEN"])
def test_a_setting_that_is_not_set_stops_the_run_and_is_named(monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(SystemExit) as stop:
        load_settings()
    assert f"Set {name} in .env" in str(stop.value)


def test_a_missing_setting_stops_the_command_before_any_call():
    env = {k: v for k, v in os.environ.items() if k != "MAX_TOOL_CALLS"}
    done = subprocess.run([sys.executable, "-m", "main.run", "deploy"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert done.returncode != 0 and "Set MAX_TOOL_CALLS in .env" in done.stderr


def test_an_empty_secret_is_allowed_until_the_call_that_needs_it(s):
    assert s.kb_mcp_token == ""
    with pytest.raises(SystemExit) as stop:
        s.need("kb_mcp_token")
    assert "Set KB_MCP_TOKEN in .env" in str(stop.value)


def test_the_older_names_are_read_when_the_new_ones_are_not_set(monkeypatch):
    for name in ("KB_MCP_TOKEN", "COURSE_GATEWAY_URL", "AWS_REGION"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("COURSE_MCP_TOKEN", "old-token")
    monkeypatch.setenv("GATEWAY_URL", "http://old.invalid/v1")
    s = load_settings()                                   # AWS_DEFAULT_REGION is set by the test overrides
    assert (s.kb_mcp_token, s.course_gateway_url, s.aws_region) == ("old-token", "http://old.invalid/v1", "ca-central-1")


def test_a_secret_is_not_in_the_printed_settings_or_in_the_runtime_environment(monkeypatch):
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-token-value")
    monkeypatch.setenv("LOGFIRE_TOKEN", "made-up-logfire-value")
    s = load_settings()
    env = s.deployed_env("https://gateway.test.invalid/mcp")
    assert "made-up" not in repr(s) and "made-up" not in str(env)
    assert env["KB_MCP_TOKEN_PARAM"] == s.token_param and env["LOGFIRE_TOKEN_PARAM"] == s.logfire_param
    assert set(env) >= {v for f, v in ENV_NAMES.items() if f not in SECRETS}     # the Runtime can build Settings


@pytest.mark.parametrize("prefix", ["recipe", "recipe-you", "team7-ana-b"])
def test_every_resource_name_starts_with_the_prefix_and_fits_the_services_pattern(monkeypatch, prefix):
    monkeypatch.setenv("PREFIX", prefix)
    s = load_settings()
    hyphen = [s.books_table, s.holds_table, s.tools_function, s.gateway_name, s.token_param, s.logfire_param,
              s.code_bucket("123456789012"), *(s.role_name(p) for p in ("lambda", "gateway", "runtime"))]
    assert all(name.startswith(f"{prefix}-agent-") for name in hyphen)
    assert s.runtime_name == prefix.replace("-", "_") + "_agent"
    pattern = lambda op, member: MODEL.operation_model(op).input_shape.members[member].metadata["pattern"]   # noqa: E731
    assert re.fullmatch(pattern("CreateAgentRuntime", "agentRuntimeName"), s.runtime_name)
    assert re.fullmatch(pattern("CreateGateway", "name"), s.gateway_name)
    assert len(s.code_bucket("123456789012")) <= 63 and len(s.role_name("gateway")) <= 64


def test_a_prefix_the_services_would_refuse_is_refused_here(monkeypatch):
    monkeypatch.setenv("PREFIX", "Recipe_You")
    with pytest.raises(SystemExit) as stop:
        load_settings()
    assert "PREFIX in .env is not valid" in str(stop.value)
