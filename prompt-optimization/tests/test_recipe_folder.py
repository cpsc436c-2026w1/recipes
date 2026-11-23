"""The folder as a student gets it: recipe.toml, the commands the README names, and what a run leaves behind.

Offline: every command here runs on the stub or a local stand-in, in a copy of this folder under a temporary
directory, so the captured runs in output/ are not touched. The checks do not read anything outside this folder
(a student copies one folder).
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
# Never from the environment in a test: a real token or URL on this machine must not reach a subprocess.
SECRETS = ("GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
           "MCP_SERVER_TOKEN", "APPROVAL_SECRET")


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SECRETS}
    return {**env, **extra}


def inventory(folder: Path) -> dict[str, str]:
    """Every file under `folder` (caches and the virtual environment left out) with a hash of its content."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not SKIP & set(path.relative_to(folder).parts):
            found[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def fresh_copy(tmp_path: Path) -> Path:
    """This folder as a clone has it: no virtual environment, no caches, nothing an earlier run left."""
    copy = tmp_path / ROOT.name
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(*SKIP, *RUN_PRODUCTS))
    return copy


def python(copy: Path, *argv: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=copy, env=env or clean_env(), capture_output=True, text=True,
                          timeout=timeout)


def readme_commands() -> list[tuple[str, str]]:
    """(file, command line) for every `uv run python ...` in a bash block of this folder's READMEs."""
    found = []
    for readme in [ROOT / "README.md", *sorted(ROOT.glob("ext-*/README.md"))]:
        for block in re.findall(r"```(?:bash)?\n(.*?)```", readme.read_text(), flags=re.S):
            for line in block.splitlines():
                line = line.strip()
                if "uv run python" in line:
                    found.append((str(readme.relative_to(ROOT)), line[line.index("uv run python"):]))
    return found


def test_recipe_toml_has_the_fields_of_the_template():
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name
    assert r["format"] in ("main-scenario", "task-class")
    for key in ("task", "cost_one_run", "summary"):
        assert isinstance(r[key], str) and r[key].strip() and "<" not in r[key], key
    for key in ("shapes", "extensions", "lectures", "core", "secondary"):
        assert isinstance(r[key], list), key
    assert r["shapes"] and r["lectures"] and r["core"]
    assert r["region"] == "ca-central-1"
    assert r["practices"] and all(p["name"].strip() and p["source"].strip() for p in r["practices"])
    assert r["instruments"]["files"]
    for rel in r["instruments"]["files"]:
        assert (ROOT / rel).exists(), f"[instruments].files names {rel}, which is not in the folder"
    assert r["validates"]["command"] == "uv run pytest -q" and r["validates"]["shows"].strip()
    assert r["commands"]["setup"] == "uv sync" and r["commands"]["test"] == "uv run pytest -q"
    assert r["commands"]["run"].startswith("uv run python")
    assert r["commands"].get("clean", "") == ("uv run python clean_up.py --yes" if (ROOT / "clean_up.py").exists() else "")
    assert r["constraints"]["no_keys_in_code"] is True
    for name, command in {**r["commands"], **r.get("extension_commands", {})}.items():
        for target in re.findall(r"uv run python (?:-m )?(\S+)", command):
            assert _target(ROOT, command, target).exists(), f"[commands].{name}: {target} is not in the folder"


def _target(base: Path, command: str, target: str) -> Path:
    """The file a `uv run python X` or `uv run python -m X` names."""
    if f"-m {target}" in command:
        return base / (target.replace(".", "/") + ".py")
    moved = re.search(r"cd (\S+) &&", command)
    return (base / moved.group(1) if moved else base) / target


def test_every_command_in_the_readmes_names_a_file_in_the_folder():
    commands = readme_commands()
    assert commands, "no `uv run python` command found in README.md"
    for readme, line in commands:
        target = re.search(r"uv run python (?:-m )?(\S+)", line).group(1)
        base = ROOT / "ext-deploy" if readme.startswith("ext-deploy") else ROOT
        assert _target(base, line, target).exists(), f"{readme}: `{line}` names {target}, which is not in the folder"
    # The commands this test file runs offline are commands the READMEs give.
    given = " ".join(line for _, line in commands)
    for argv in README_GIVES:
        assert "uv run python " + " ".join(argv) in given, argv


# --- this recipe ---
RUN_PRODUCTS = ("runs",)                                  # what a run writes (README, Cost and clean-up)
README_GIVES = [["-m", "main.run"], ["ext-gepa-direct/optimize_direct.py"], ["ext-eval-matrix/matrix.py"],
                ["ext-eval-matrix/matrix.py", "--case", "rides"], ["ext-eval-matrix/groups.py"], ["clean_up.py"], ["clean_up.py", "--yes"]]


def test_offline_runs_twice_and_clean_up_leaves_nothing(tmp_path):
    copy = fresh_copy(tmp_path)
    before = inventory(copy)
    for attempt in ("first run", "run right after the clean-up"):
        main = python(copy, "-m", "main.run", "--runs", "1")
        assert main.returncode == 0, main.stderr[-2000:]
        assert "| after |" in main.stdout and "The search ended at:" in main.stdout
        again = python(copy, "-m", "main.run", "--runs", "1", "--max-reflection-calls", "1")   # a second run
        assert again.returncode == 0, again.stderr[-2000:]
        direct = python(copy, "ext-gepa-direct/optimize_direct.py", "--runs", "1")
        assert direct.returncode == 0, direct.stderr[-2000:]
        after_file = next((copy / "runs").glob("2*/instructions_after.txt"))
        matrix = python(copy, "ext-eval-matrix/matrix.py", "--runs", "1", "--prompt", f"after={after_file}")
        assert matrix.returncode == 0, matrix.stderr[-2000:]
        rides = python(copy, "ext-eval-matrix/matrix.py", "--case", "rides", "--runs", "1",
                       "--compare", "qwen3.6-35b-a3b/seed,qwen3.8-27b/seed", "--out", str(copy / "runs" / "rides"))
        assert rides.returncode == 0, rides.stderr[-2000:]
        saved = copy / "runs" / "rides" / "cases.jsonl"
        again = python(copy, "ext-eval-matrix/groups.py", str(saved), "--compare", "qwen3.6-35b-a3b/seed,qwen3.8-27b/seed")
        assert again.returncode == 0 and "| all |" in again.stdout, again.stderr[-2000:]
        created = set(inventory(copy)) - set(before)
        assert created and all(name.startswith("runs/") for name in created), sorted(created)[:5]
        clean = python(copy, "clean_up.py", "--yes")
        assert clean.returncode == 0 and "removed" in clean.stdout, attempt
        assert inventory(copy) == before, attempt
    assert "nothing to remove" in python(copy, "clean_up.py", "--yes").stdout     # a clean-up with nothing left still succeeds


def test_gateway_run_without_the_environment_stops_before_any_call(tmp_path):
    copy = fresh_copy(tmp_path)
    for argv in (["-m", "main.run", "--target", "gateway"],
                 ["ext-gepa-direct/optimize_direct.py", "--target", "gateway"],
                 ["ext-eval-matrix/matrix.py", "--target", "gateway"]):
        done = python(copy, *argv)
        assert done.returncode != 0 and "set COURSE_GATEWAY_URL" in done.stderr and "Traceback" not in done.stderr, argv
    assert not (copy / "runs").exists()
