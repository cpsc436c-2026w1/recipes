#!/usr/bin/env python3
"""Build index.json from every recipe.toml, for tools and coding assistants (for example an MCP tool `find_recipe`).

    python3 tools/build_index.py          # writes index.json at the repo root

Each entry carries the fields an assistant needs to choose a recipe and point a student at it: name, summary, task,
format, project shapes and extensions it serves, lectures, the practices it teaches (each with its source), the
reusable files, how to validate it, the commands and the cost of one run. The README of the folder is the recipe;
`readme` links to it.
"""
import json
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEEP = ["name", "summary", "task", "format", "shapes", "extensions", "lectures", "core", "secondary",
        "practices", "instruments", "validates", "commands", "cost_one_run", "region", "constraints"]

entries = []
for toml in sorted(ROOT.glob("*/recipe.toml")):
    folder = toml.parent.name
    if folder.startswith("_"):
        continue  # the template is not a recipe
    data = tomllib.loads(toml.read_text())
    entry = {k: data[k] for k in KEEP if k in data}
    entry["folder"] = folder
    entry["readme"] = f"{folder}/README.md"
    entries.append(entry)

(ROOT / "index.json").write_text(json.dumps({"recipes": entries}, indent=2, ensure_ascii=False) + "\n")
print(f"index.json: {len(entries)} recipes")
