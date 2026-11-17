"""The folder's own contract: recipe.toml has the fields tools read, and the README's commands exist and run."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_recipe_toml_has_the_fields_tools_read():
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name and r["region"] == "ca-central-1"
    assert r["format"] in ("main-scenario", "task-class")
    for key in ("task", "cost_one_run", "summary"):
        assert isinstance(r[key], str) and r[key].strip(), key
    for key in ("shapes", "extensions", "lectures", "core", "secondary"):
        assert isinstance(r[key], list), key
    assert r["practices"] and all(p["name"].strip() and p["source"].strip() for p in r["practices"])
    assert r["instruments"]["files"] and all((ROOT / f).is_file() for f in r["instruments"]["files"])
    assert r["validates"]["command"].strip() and r["validates"]["shows"].strip()
    assert r["commands"]["setup"] == "uv sync" and r["commands"]["test"] == "uv run pytest -q"
    for key in ("run", "clean"):
        assert script_of(r["commands"][key]).is_file(), key
    for command in r["extension_commands"].values():
        assert script_of(command).is_file(), command
    assert r["constraints"]["no_keys_in_code"] is True


def readme_commands(heading: str) -> list[str]:
    section = (ROOT / "README.md").read_text().split(f"## {heading}")[1].split("\n## ")[0]
    lines = [ln for block in re.findall(r"```bash\n(.*?)```", section, re.S) for ln in block.splitlines()]
    return [re.split(r"\s+#\s", ln)[0].strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def script_of(command: str) -> Path:
    """`uv run python main/run.py --target aws` -> main/run.py; `uv run python -m main.run` -> main/run.py."""
    words = shlex.split(command)
    assert words[:3] == ["uv", "run", "python"], command
    return ROOT / (words[4].replace(".", "/") + ".py" if words[3] == "-m" else words[3])


def test_readme_commands_exist_and_the_offline_one_runs():
    commands = readme_commands("Run it") + readme_commands("Cost and clean-up")
    scripts = [c for c in commands if c.startswith("uv run python")]
    assert len(scripts) >= 6  # stub run, gateway run, two AWS runs, clean-up dry run, clean-up
    for c in scripts:
        assert script_of(c).is_file(), c
    for c in commands:
        first = c.split()[0]
        # a command, or a variable set for one command (APP_MODEL=stub, DRY=1) or for the next ones (MY_IP=$(...))
        # "[" is the test in front of the fall-back to the default VPC; echo shows what was found; tar copies the folder
        assert first in ("cd", "uv", "aws", "export", "read", "bash", "curl", "sudo", "[", "echo", "tar") or "=" in first, c
    for c in commands:  # every shell script and every Shiny app a command names is in the folder
        for path in re.findall(r"\bbash ((?:ext-|main/)[\w./-]+\.sh)", c) + re.findall(r"shiny run ([\w./-]+\.py)", c):
            assert (ROOT / path).is_file(), c
    assert sum("fargate/" in c for c in commands) >= 6 and sum("shiny run" in c for c in commands) == 3
    # Offline here: the stub model, and the clean-up dry run (it builds the names and contacts nothing).
    offline = [c for c in scripts if "--model stub" in c or "--dry-run" in c]
    assert len(offline) == 2
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
           "AWS_SESSION_TOKEN": "testing", "AWS_REGION": "ca-central-1", "PREFIX": "test",
           "GATEWAY_URL": "", "COURSE_MCP_TOKEN": "", "COURSE_GATEWAY_URL": "", "KB_MCP_TOKEN": ""}
    env.pop("AWS_PROFILE", None)
    for c in offline:
        done = subprocess.run([sys.executable, *shlex.split(c)[3:]], cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip()
