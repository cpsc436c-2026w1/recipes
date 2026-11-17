"""README "Run it": every script it names exists and runs under DRY=1 (prints its commands, calls nothing), and
recipe.toml has every field a tool or a coding assistant reads. The field list is written out here: a copied folder
has no template beside it."""

import re
import tomllib

import pytest

from conftest import RECIPE


def run_it_scripts() -> list[str]:
    """The scripts of this recipe that the `bash <script>` commands of the README's "Run it" section name."""
    section = (RECIPE / "README.md").read_text().split("\n## Run it\n")[1].split("\n## ")[0]
    found = []
    for script in re.findall(r"\bbash ([\w./-]+\.sh)", section):
        if script not in found:
            found.append(script)
    return found


SCRIPTS = run_it_scripts()


def test_run_it_names_scripts():
    assert "main/run.sh" in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS)
def test_each_run_it_script_exists_and_runs_dry(script, dry):
    assert (RECIPE / script).is_file(), script
    out = dry(script, BUCKET="my-bucket").stdout              # the dry fixture fails the test if aws is called
    assert "$ aws " in out


def test_the_dry_form_is_in_the_readme():
    section = (RECIPE / "README.md").read_text().split("\n## Run it\n")[1].split("\n## ")[0]
    assert "DRY=1 bash main/run.sh" in section


def test_recipe_toml_has_every_field():
    r = tomllib.loads((RECIPE / "recipe.toml").read_text())
    assert r["name"] == RECIPE.name
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
        assert (RECIPE / f).exists(), f
    assert r["validates"]["command"] and r["validates"]["shows"]
    assert r["commands"]["setup"] == "uv sync" and r["commands"]["test"] == "uv run pytest -q"
    assert r["commands"]["run"] and r["commands"]["clean"]              # this recipe has both
    assert r["constraints"]["no_keys_in_code"] is True


def test_the_commands_in_recipe_toml_name_files_that_exist():
    commands = tomllib.loads((RECIPE / "recipe.toml").read_text())["commands"]
    for name, command in commands.items():
        for script in re.findall(r"\bbash ([\w./-]+\.sh)", command):
            assert (RECIPE / script).is_file(), f"{name}: {command}"
