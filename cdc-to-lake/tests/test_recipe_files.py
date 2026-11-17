"""The recipe's own files follow the format (_template/README.md)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = ["What you build", "Use it when", "Main scenario", "Decisions", "Requirements", "Run it", "What you should see",
            "Extensions", "Cost and clean-up", "Adapt it"]
FIELDS = ["name", "format", "task", "shapes", "extensions", "lectures", "core", "secondary", "region",
          "cost_one_run", "summary", "practices", "instruments", "validates", "commands", "constraints"]


def test_readme_has_the_ten_headings_in_order():
    found = re.findall(r"^## (.+)$", (ROOT / "README.md").read_text(), flags=re.M)
    assert found == HEADINGS


def test_recipe_toml_has_every_field_and_real_paths():
    d = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert [f for f in FIELDS if f not in d] == []
    assert d["name"] == ROOT.name and d["region"] == "ca-central-1"
    assert all(p["source"] for p in d["practices"])
    assert [f for f in d["instruments"]["files"] if not (ROOT / f).exists()] == []
    assert set(d["commands"]) >= {"setup", "test", "run", "clean"}


def test_no_bytes_read_wording():
    for f in ROOT.rglob("*.md"):
        if ".venv" not in f.parts:
            assert not re.search(r"bytes (read|scanned)", f.read_text(), flags=re.I), f
