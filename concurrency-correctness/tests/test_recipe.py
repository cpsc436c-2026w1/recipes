"""The recipe's shape: the ten README headings in order, a filled recipe.toml whose paths exist."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = ["What you build", "Use it when", "Main scenario", "Decisions", "Requirements", "Run it", "What you should see",
            "Extensions", "Cost and clean-up", "Adapt it"]


def test_readme_has_the_ten_headings_in_order():
    assert re.findall(r"^## (.+)$", (ROOT / "README.md").read_text(), re.MULTILINE) == HEADINGS


def test_recipe_toml_is_filled_and_its_paths_exist():
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name and r["region"] == "ca-central-1"
    for k in ("task", "summary", "lectures", "core", "cost_one_run"):
        assert r[k] and "<" not in str(r[k]), k
    assert r["practices"] and all(p["name"] and p["source"] for p in r["practices"])
    for f in r["instruments"]["files"] + list(r.get("scenarios", {}).values()):
        assert (ROOT / f).exists(), f
    assert r["validates"]["command"] and r["validates"]["shows"]
    assert {"setup", "test", "run", "clean"} <= set(r["commands"])
    assert r["constraints"]["name_prefix_env"] == "PREFIX"
