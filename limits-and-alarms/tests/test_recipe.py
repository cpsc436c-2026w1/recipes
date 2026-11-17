"""The folder's own contract: recipe.toml has the fields tools read, and the README's commands exist and run."""

from __future__ import annotations

import os
import re
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
    assert r["constraints"]["no_keys_in_code"] is True


def readme_commands(heading: str) -> list[str]:
    section = (ROOT / "README.md").read_text().split(f"## {heading}")[1].split("\n## ")[0]
    lines = [ln for block in re.findall(r"```bash\n(.*?)```", section, re.S) for ln in block.splitlines()]
    return [re.split(r"\s+#\s", ln)[0].strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def script_of(command: str) -> Path:
    """`uv run python main/run.py --target aws` -> main/run.py; `uv run python -m main.run` -> main/run.py."""
    words = command.split()
    assert words[:3] == ["uv", "run", "python"], command
    return ROOT / (words[4].replace(".", "/") + ".py" if words[3] == "-m" else words[3])


def test_readme_commands_exist_and_the_offline_one_runs():
    commands = readme_commands("Run it") + readme_commands("Cost and clean-up")
    scripts = [c for c in commands if c.startswith("uv run python")]
    assert len(scripts) >= 6  # offline run, AWS run, fire, fire --synthetic, clean-up dry run, clean-up
    for c in scripts:
        assert script_of(c).is_file(), c
    for c in commands:
        assert c.split()[0] in ("cd", "uv", "aws", "export", "read"), c
    offline = [c for c in scripts if "--target local" in c]
    assert offline
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
           "AWS_SESSION_TOKEN": "testing", "AWS_REGION": "ca-central-1", "PREFIX": "test"}
    env.pop("AWS_PROFILE", None)
    for c in offline:
        done = subprocess.run([sys.executable, *c.split()[3:]], cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip()
