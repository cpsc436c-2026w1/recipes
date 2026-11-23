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
RUN_PRODUCTS = ("data", "refunds.duckdb", "refunds.duckdb.wal")   # what a run writes ([resources].local_files)
README_GIVES = [["-m", "main.server"], ["-m", "main.client"], ["ext-confirmed-write/write_server.py"],
                ["ext-confirmed-write/walk.py", "--approver", "ana"], ["clean_up.py"], ["clean_up.py", "--yes"]]
TOKEN = "token-for-this-test-only-0123456789"
SECRET = "approval-secret-for-this-test-only-01"


def _free_port() -> str:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def _serve(copy: Path, env: dict, *argv: str) -> subprocess.Popen:
    """Start a server command from the README and wait for the line it prints when it is up."""
    proc = subprocess.Popen([sys.executable, "-u", *argv], cwd=copy, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    assert "MCP server" in line and "/mcp" in line, line + (proc.stdout.read() if proc.poll() is not None else "")
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc.stdout.close()


def test_the_readme_commands_run_twice_and_clean_up_leaves_nothing(tmp_path):
    copy = fresh_copy(tmp_path)
    before = inventory(copy)
    for attempt in ("first run", "run right after the clean-up"):
        env = clean_env(MCP_SERVER_TOKEN=TOKEN, APPROVAL_SECRET=SECRET, MCP_PORT=_free_port())
        server = _serve(copy, env, "-m", "main.server")
        try:
            for _ in range(2):                                            # the client twice against one server
                client = python(copy, "-m", "main.client", env=env)
                assert client.returncode == 0, client.stdout + client.stderr[-2000:]
            wrong = python(copy, "-m", "main.client", env={**env, "MCP_SERVER_TOKEN": "not-the-server-token"})
            assert wrong.returncode != 0 and TOKEN not in wrong.stdout + wrong.stderr
        finally:
            _stop(server)
        env["MCP_PORT"] = _free_port()
        writer = _serve(copy, env, "ext-confirmed-write/write_server.py")
        try:
            for _ in range(2):                                            # the second walk finds the refund applied
                walk = python(copy, "ext-confirmed-write/walk.py", "--approver", "ana", env=env)
                assert walk.returncode == 0, walk.stdout + walk.stderr[-2000:]
            assert '"status": "already_applied"' in walk.stdout and SECRET not in walk.stdout
        finally:
            _stop(writer)
        created = set(inventory(copy)) - set(before)
        assert {"data/orders.parquet", "data/stores.parquet", "output/refunds.duckdb"} <= created, attempt
        dry = python(copy, "clean_up.py")
        assert set(re.findall(r"would delete (\S+)", dry.stdout)) == created
        assert set(inventory(copy)) - set(before) == created              # a dry run deletes nothing
        clean = python(copy, "clean_up.py", "--yes")
        assert clean.returncode == 0 and set(re.findall(r"^deleted (\S+)", clean.stdout, flags=re.M)) == created
        assert inventory(copy) == before, attempt
    assert python(copy, "clean_up.py", "--yes").returncode == 0                    # nothing left: still succeeds


def test_servers_refuse_to_start_without_their_secrets(tmp_path):
    copy = fresh_copy(tmp_path)
    no_token = python(copy, "-m", "main.server", env=clean_env(MCP_PORT=_free_port()))
    assert no_token.returncode != 0 and "MCP_SERVER_TOKEN" in no_token.stderr
    no_secret = python(copy, "ext-confirmed-write/write_server.py",
                       env=clean_env(MCP_SERVER_TOKEN=TOKEN, MCP_PORT=_free_port()))
    assert no_secret.returncode != 0 and "APPROVAL_SECRET" in no_secret.stderr
