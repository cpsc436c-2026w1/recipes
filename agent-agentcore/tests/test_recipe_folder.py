"""The folder as a student gets it: recipe.toml, the commands the README names, the captured runs, the wording."""
import re
import subprocess
import sys
import tomllib

import pytest

from tests.conftest import ROOT

README = (ROOT / "README.md").read_text()
HEADINGS = ["What you build", "Use it when", "Main scenario", "Decisions", "Requirements", "Run it",
            "What you should see", "Extensions", "Cost and clean-up", "Adapt it"]


def test_the_readme_has_the_templates_headings_in_order():
    assert re.findall(r"^## (.+)$", README, flags=re.M) == HEADINGS


def test_recipe_toml_names_files_that_exist_and_commands_the_readme_has():
    recipe = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert recipe["name"] == ROOT.name and recipe["region"] == "ca-central-1"
    assert [f for f in recipe["instruments"]["files"] if not (ROOT / f).exists()] == []
    for name in ("test", "scenarios", "eval", "run_offline", "run", "invoke", "clean"):
        assert recipe["commands"][name] in README, name


def test_every_uv_run_python_command_of_the_readme_names_an_env_file():
    commands = [line.strip() for block in re.findall(r"```bash\n(.*?)```", README, flags=re.S)
                for line in block.splitlines() if "uv run" in line and ("python" in line or "inspect" in line)]
    assert len(commands) >= 8
    assert [c for c in commands if "--env-file .env" not in c] == []


@pytest.mark.parametrize("argv", [
    ["-m", "main.run", "ask", "--model", "stub", "--yes", "--request-id", "req-1"],
    ["-m", "main.run", "ask", "--model", "stub", "--no", "--request-id", "req-1"],
    ["-m", "main.run", "ask", "--model", "stub", "--stub", "runaway", "--yes", "--request-id", "req-1"],
    ["-m", "main.run", "ask", "--model", "stub", "--stub", "wrong_arguments", "--yes", "--request-id", "req-1"],
])
def test_the_captured_offline_runs_are_what_the_commands_print_now(argv):
    done = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, timeout=120)
    captured = (ROOT / "output" / "main-local.txt").read_text()
    command = "$ uv run --env-file .env.example python " + " ".join(argv)
    assert command in captured
    block = captured.split(command + "\n", 1)[1].split("\n\n", 1)[0]
    assert block == done.stdout.strip() + f"\nexit code {done.returncode}"


def test_no_source_label_says_ours_and_no_key_or_account_is_in_the_folder():
    skip = {".venv", "__pycache__", ".pytest_cache", "build", "logs", "uv.lock"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip & set(path.relative_to(ROOT).parts) or path.suffix in (".pyc",):
            continue
        text = path.read_text(errors="ignore")
        if path.name != "test_recipe_folder.py":
            assert not re.search(r"\(ours\)|src: ?ours|ours:", text), path
        assert not re.search(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b", text), path
        accounts = set(re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", text)) - {"123456789012", "111122223333"}
        assert not accounts, (path, accounts)


def test_the_readme_keeps_the_wording_rulings():
    prose = re.sub(r"```.*?```|<!--.*?-->|`[^`]*`", "", README, flags=re.S)
    assert not re.search(r"\b(keeps|has|gets|takes|makes|carries|holds|needs) no \w+", prose), "negate the verb"
    assert not re.search(r"system message|tool (message|output|response)\b", prose), "one LLM vocabulary"
    assert not re.search(r"\bcosts? (time|delay|latency)", prose), "cost means money"
    assert "the grocery" not in prose.lower() and "shopper" not in prose.lower()


def test_the_example_domain_is_named_in_one_place_each():
    """A team replaces the tools in contracts, tools, store and the data file; the hooks name no tool of the example
    except through WRITE_TOOLS and the approval's reason."""
    hooks = (ROOT / "main" / "hooks.py").read_text()
    assert "search_books" not in hooks and "get_holds" not in hooks
