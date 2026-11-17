"""The recipe's shape: the ten README headings in order, a filled recipe.toml, no secrets in the files."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = ["What you build", "Use it when", "Main scenario", "Decisions", "Requirements", "Run it", "What you should see",
            "Extensions", "Cost and clean-up", "Adapt it"]


def test_readme_has_the_ten_headings_in_order():
    found = re.findall(r"^## (.+)$", (ROOT / "README.md").read_text(), re.MULTILINE)
    assert found == HEADINGS


def test_recipe_toml_is_filled():
    r = tomllib.loads((ROOT / "recipe.toml").read_text())
    assert r["name"] == ROOT.name and r["format"] == "task-class" and r["region"] == "ca-central-1"
    assert set(r["shapes"]) == {"lakehouse-first", "operational-first", "genai-first"}
    for k in ("task", "lectures", "core", "cost_one_run", "summary"):
        assert r[k] and "<" not in str(r[k])
    assert r["practices"] and all(p["name"] and p["source"] for p in r["practices"])
    for f in r["instruments"]["files"]:
        assert (ROOT / f).exists(), f
    assert r["validates"]["command"] and r["validates"]["shows"]
    for folder in r["representatives"].values():
        assert (ROOT / folder / "README.md").exists() and (ROOT / folder / "run.py").exists()
    assert {"setup", "test", "run", "clean"} <= set(r["commands"])
    assert r["constraints"]["name_prefix_env"] == "PREFIX"


def test_no_keys_in_the_recipe():
    pattern = re.compile(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|aws_secret_access_key\s*=|-----BEGIN [A-Z ]*PRIVATE KEY")
    for path in ROOT.rglob("*"):
        if path.is_file() and not {".venv", ".local", "__pycache__", ".pytest_cache"} & set(path.parts) \
                and path.suffix in {".py", ".md", ".toml", ".json", ".txt", ".yml", ".yaml"}:
            assert not pattern.search(path.read_text()), path


def test_every_name_starts_with_the_prefix():
    from skeleton.config import names
    for key, value in names("abc", account="111122223333").items():
        assert value.startswith(("abc", "/aws/lambda/abc")), key
