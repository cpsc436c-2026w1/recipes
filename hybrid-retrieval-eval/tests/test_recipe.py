"""The recipe's own files agree with each other: every command the README and recipe.toml give names a file that
exists, the offline commands run, and recipe.toml has the fields tools read (tools/build_index.py, coding assistants).
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
RECIPE = tomllib.loads((ROOT / "recipe.toml").read_text())

HEADINGS = ["What you build", "Use it when", "Main scenario", "Decisions", "Requirements", "Run it", "What you should see",
            "Extensions", "Cost and clean-up", "Adapt it"]


def section(title: str) -> str:
    return README.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def commands(text: str) -> list[str]:
    """The `uv run ...` lines of the bash blocks in a README section, without their comments."""
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    lines = [line.split("  #")[0].strip() for b in blocks for line in b.splitlines()]
    return [re.sub(r"^(?:[A-Z_]+=\S+\s+)+", "", line) for line in lines if "uv run" in line]


def target(command: str) -> Path | None:
    """The file a `uv run python ...` command starts: a script path, or the module after -m."""
    m = re.search(r"uv run python (?:-m ([\w.]+)|(\S+\.py))", command)
    if not m:
        return None
    return ROOT / (m.group(1).replace(".", "/") + ".py" if m.group(1) else m.group(2))


README_COMMANDS = commands(section("Run it")) + commands(section("Cost and clean-up"))
TOML_COMMANDS = [v for k, v in RECIPE["commands"].items() if k != "setup"] + list(
    RECIPE.get("extension_commands", {}).values())


def test_the_readme_has_the_ten_headings_in_order():
    assert re.findall(r"^## (.+)$", README, flags=re.M) == HEADINGS


def test_the_readme_gives_commands_to_run_and_to_clean_up():
    assert any("main.run" in c for c in README_COMMANDS) and any("clean_up.py" in c for c in README_COMMANDS)
    assert "uv run pytest -q" in README_COMMANDS


@pytest.mark.parametrize("command", sorted(set(README_COMMANDS + TOML_COMMANDS)))
def test_every_command_names_a_file_that_exists(command):
    for part in command.split("&&"):
        if "uv run python" in part:
            assert target(part) and target(part).is_file(), f"{part.strip()!r}: no such file in this folder"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120)


def test_the_offline_run_of_the_readme_runs(tmp_path, monkeypatch, capsys):
    """`uv run python -m main.run`: stub embeddings and answers, local indexes. Its file goes to a temporary
    folder here, so a test run leaves output/ as it is."""
    import main.run

    assert "uv run python -m main.run" in README_COMMANDS
    monkeypatch.setattr(main.run, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.run, "ROOT", tmp_path)
    main.run.main([])
    out = capsys.readouterr().out
    assert "| hybrid |" in out and (tmp_path / "eval-local-stub-stub.json").is_file()


@pytest.mark.parametrize("script", ["ext-filters/filters_run.py", "ext-model-routing/routing_eval.py"])
def test_the_offline_form_of_each_extension_runs(script):
    done = run(script)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip()


def test_clean_up_explains_itself_without_an_account():
    done = run("clean_up.py", "--help")
    assert done.returncode == 0 and "--dry-run" in done.stdout


def test_recipe_toml_has_the_template_fields():
    for key in ("name", "format", "task", "shapes", "extensions", "lectures", "core", "secondary", "region",
                "cost_one_run", "summary", "practices", "instruments", "validates", "commands", "constraints"):
        assert RECIPE.get(key), f"recipe.toml: {key} is missing or empty"
    assert RECIPE["name"] == ROOT.name and RECIPE["region"] == "ca-central-1"
    assert RECIPE["format"] in ("main-scenario", "task-class")
    assert set(RECIPE["shapes"]) <= {"lakehouse-first", "operational-first", "genai-first"}
    assert all(p.get("name") and p.get("source") for p in RECIPE["practices"])
    assert RECIPE["validates"].get("command") and RECIPE["validates"].get("shows")
    assert RECIPE["commands"]["setup"] == "uv sync" and RECIPE["commands"]["test"] == "uv run pytest -q"
    assert RECIPE["commands"].get("run") and RECIPE["commands"].get("clean")
    assert RECIPE["constraints"]["no_keys_in_code"] is True and RECIPE["constraints"]["name_prefix_env"] == "PREFIX"
    assert RECIPE["constraints"]["model_calls_through"] == "course LLM gateway"


def test_the_reusable_files_exist():
    missing = [f for f in RECIPE["instruments"]["files"] if not (ROOT / f).is_file()]
    assert not missing, f"recipe.toml [instruments].files not in this folder: {missing}"


def test_the_readme_does_not_state_the_gateway_s_budget_or_rate_numbers():
    """Course constraint: the README sends students to the course page for their key's limits."""
    for text in [README] + [p.read_text() for p in ROOT.glob("ext-*/README.md")]:
        assert not re.search(r"\d+\s*(?:requests|calls)\s*(?:a|per)\s*minute", text, flags=re.I)
        assert not re.search(r"\b\d+(?:\.\d+)?\s*CAD\s*(?:per|a)\s*key\b|budget of \d", text, flags=re.I)
