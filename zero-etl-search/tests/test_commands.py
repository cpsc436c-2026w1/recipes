"""Every command the READMEs and recipe.toml give names a file in this folder, and the offline ones run."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RECIPE = tomllib.loads((ROOT / "recipe.toml").read_text())


def commands(text: str) -> list[str]:
    """Every `uv run ...` command in a README: in a bash block or in back-quotes, without its comment or its
    `NAME=value` prefix."""
    found = re.findall(r"^(?:[A-Z_]+=\S+\s+)*(uv run [^#\n]+)", text, flags=re.M)
    found += re.findall(r"`(?:[A-Z_]+=\S+\s+)*(uv run [^`]+)`", text)
    return sorted({c.strip() for c in found})


def target(command: str) -> Path | None:
    """The file a `uv run python ...` or `uv run marimo ...` command starts: a script path, or the module after -m."""
    m = re.search(r"uv run (?:python|marimo \w+(?: html)?(?: --[\w-]+)*) (?:-m ([\w.]+)|(\S+\.py))", command)
    if not m:
        return None
    return ROOT / (m.group(1).replace(".", "/") + ".py" if m.group(1) else m.group(2))


README_COMMANDS = sorted({c for f in ("README.md", "ext-embeddings/README.md", "ext-zero-etl/README.md")
                          for c in commands((ROOT / f).read_text())})
TOML_COMMANDS = [v for k, v in RECIPE["commands"].items() if k != "setup"] + list(
    RECIPE.get("extension_commands", {}).values())


def test_the_readmes_give_the_commands_of_the_recipe():
    for needed in ("uv run pytest -q", "uv run python -m main.run", "uv run python -m main.run --target aws",
                   "uv run python clean_up.py", "uv run python clean_up.py --yes",
                   "uv run python ext-embeddings/worker.py", "uv run python ext-zero-etl/zero_etl.py",
                   "uv run marimo edit demo/search_demo.py", "uv run marimo edit demo/search_demo.py -- --target aws",
                   "uv run python ext-zero-etl/pipeline.py --write"):
        assert needed in README_COMMANDS, needed
    assert set(RECIPE["commands"].values()) - {"uv sync"} <= set(README_COMMANDS)


@pytest.mark.parametrize("command", sorted(set(README_COMMANDS + TOML_COMMANDS)))
def test_every_command_names_a_file_that_exists(command):
    if "uv run pytest" in command or command == "uv run behave":
        return
    assert target(command) and target(command).is_file(), f"{command!r}: no such file in this folder"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120)


def test_the_offline_main_scenario_runs():
    done = run("-m", "main.run")
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.startswith("[local: moto DynamoDB and Streams, stand-in event source mapping and index]")
    assert "failure queue: 1 message(s), batch size [1], item ['I11']" in done.stdout


def test_the_offline_extension_runs():
    done = run("ext-embeddings/worker.py")
    assert done.returncode == 0, done.stderr[-2000:]
    assert "dead-letter queue: ['I08']" in done.stdout
    assert "failed, left in the queue: RuntimeError: embedding call failed" in done.stderr   # the reason is said


def test_the_zero_etl_extension_runs_offline_and_renders_its_example():
    done = run("ext-zero-etl/zero_etl.py")
    assert done.returncode == 0 and "mappings and pipeline definition: valid" in done.stdout, done.stderr[-2000:]
    done = run("ext-zero-etl/pipeline.py")
    assert done.returncode == 0 and "dynamodb" in done.stdout, done.stderr[-2000:]


def test_the_demo_notebook_checks_and_runs_offline():
    check = subprocess.run([sys.executable, "-m", "marimo", "check", "demo/search_demo.py"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
    assert check.returncode == 0, check.stdout + check.stderr
    done = run("demo/search_demo.py")                    # every cell, headless, against the offline stand-ins
    assert done.returncode == 0, done.stderr[-2000:]


def test_clean_up_explains_itself_without_an_account():
    done = run("clean_up.py", "--help")
    assert done.returncode == 0 and "--dry-run" in done.stdout and "FAILED" in done.stdout
