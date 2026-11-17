"""README "Run it": every script it names exists, its offline form runs, and recipe.toml has every field a tool
or a coding assistant reads. The field list is written out here: a copied folder has no template beside it."""

import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# These forms need an AWS account or the course gateway. They are never run here: clean_up.py deletes for real.
NOT_OFFLINE = ("--target aws", "--llm gateway", "clean_up.py")
# The offline forms run with made-up credentials and no instance metadata, so none of them can reach an account.
NO_ACCOUNT = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
              "AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "ca-central-1"}


def run_it_commands() -> list[str]:
    """The `uv run python ...` commands of the README's "Run it" section: in its code blocks (comments removed)
    and written in the text between backticks."""
    section = (ROOT / "README.md").read_text().split("\n## Run it\n")[1].split("\n## ")[0]
    found = re.findall(r"`uv run python ([^`\n]+)`", section)
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", section, re.DOTALL):
        for line in block.splitlines():
            line = line.split("#")[0].strip()
            if "uv run python " in line:
                found.append(line[line.index("uv run python ") + len("uv run python "):])
    return found


def script_of(command: str) -> Path:
    words = shlex.split(command)
    return ROOT / (words[1].replace(".", "/") + ".py" if words[0] == "-m" else words[0])


def run(command: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PREFIX", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "AWS_PROFILE")} | NO_ACCOUNT
    return subprocess.run([sys.executable, *shlex.split(command)], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=120)


COMMANDS = run_it_commands()


def test_run_it_names_commands():
    assert COMMANDS, "no `uv run python` command found in README, Run it"


@pytest.mark.parametrize("command", COMMANDS)
def test_the_script_of_each_run_it_command_exists(command):
    assert script_of(command).is_file(), command


@pytest.mark.parametrize("command", [c for c in COMMANDS if not any(w in c for w in NOT_OFFLINE)])
def test_each_offline_run_it_command_runs(command):
    done = run(command)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("command", [c for c in COMMANDS if any(w in c for w in NOT_OFFLINE)
                                     and "clean_up.py" not in c and "--" in c])
def test_each_aws_run_it_command_takes_its_options(command):
    """Not run (no account here): the script must at least know the options the README gives it. Only --help is
    asked of it, and only of a script that is given options (so it reads them); clean_up.py is not started."""
    words = shlex.split(command)
    script = words[:2] if words[0] == "-m" else words[:1]
    help_text = run(shlex.join(script + ["--help"]))
    assert help_text.returncode == 0, help_text.stderr
    for option in [w for w in words if w.startswith("--")]:
        assert option in help_text.stdout, f"{option} is not an option of {script}"


def test_recipe_toml_has_every_field():
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name
    assert r["format"] in ("main-scenario", "task-class")
    assert r["region"] == "ca-central-1"
    for key in ("task", "cost_one_run", "summary"):
        assert isinstance(r[key], str) and r[key] and "<" not in r[key], key
    for key in ("shapes", "extensions", "lectures", "core", "secondary"):
        assert isinstance(r[key], list), key
    assert r["shapes"] and r["lectures"] and r["core"]
    assert r["practices"] and all(p["name"] and p["source"] for p in r["practices"])
    assert r["instruments"]["files"]
    for f in r["instruments"]["files"]:
        assert (ROOT / f).exists(), f
    assert r["validates"]["command"] and r["validates"]["shows"]
    assert r["commands"]["setup"] == "uv sync" and r["commands"]["test"] == "uv run pytest -q"
    assert r["commands"]["run"] and r["commands"]["clean"]              # this recipe has both
    assert r["constraints"]["no_keys_in_code"] is True


def test_the_commands_in_recipe_toml_name_files_that_exist():
    commands = tomllib.loads((ROOT / "recipe.toml").read_text())["commands"]
    for name, command in commands.items():
        if "uv run python " in command:
            assert script_of(command.split("uv run python ")[1]).is_file(), f"{name}: {command}"
