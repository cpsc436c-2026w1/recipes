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
    assert r["commands"].get("clean", "") == ("uv run python clean_up.py" if (ROOT / "clean_up.py").exists() else "")
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
RUN_PRODUCTS = ()                                          # a run creates nothing unless --json names a file
README_GIVES = [["main/probe.py"], ["main/probe.py", "--groups", "main"]]


def test_the_probe_without_the_environment_stops_before_any_request(tmp_path):
    copy = fresh_copy(tmp_path)
    before = inventory(copy)
    done = python(copy, "main/probe.py", "--groups", "main")
    assert done.returncode != 0 and "Set LLM_BASE_URL and LLM_API_KEY" in done.stderr
    assert "Traceback" not in done.stderr
    assert inventory(copy) == before


def test_a_run_against_a_local_endpoint_twice_leaves_nothing_but_the_json_file(tmp_path):
    """The README's commands against tests/fake_endpoint.py, served on a local port: every group, then main only."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import httpx

    sys.path.insert(0, str(ROOT / "tests"))
    import fake_endpoint

    answer = fake_endpoint.handler().handler          # the function behind the tests' httpx.MockTransport

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            r = answer(httpx.Request(self.command, f"http://local{self.path}", content=body,
                                     headers={"authorization": self.headers.get("Authorization", "")}))
            data = r.read()
            self.send_response(r.status_code)
            self.send_header("Content-Type", r.headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        copy = fresh_copy(tmp_path)
        before = inventory(copy)
        env = clean_env(LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", LLM_API_KEY=fake_endpoint.KEY)
        for argv in (["main/probe.py"], ["main/probe.py", "--groups", "main", "--json", "rows.json"]):
            done = python(copy, *argv, env=env)
            assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
            assert "| list models |  | yes |" in done.stdout and fake_endpoint.KEY not in done.stdout
        assert set(inventory(copy)) - set(before) == {"rows.json"}
        assert fake_endpoint.KEY not in (copy / "rows.json").read_text()
    finally:
        server.shutdown()
