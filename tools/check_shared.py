#!/usr/bin/env python3
"""Compare the copies of helpers that are meant to be the same in several recipe folders.

    python3 tools/check_shared.py            # exit 1 when a copy differs
    python3 tools/check_shared.py --list     # what is compared, and where

A recipe folder is self-contained: a student copies one folder, so a helper that two recipes need is copied, not
imported. Copies drift. `tools/shared_helpers.toml` names the helpers that are meant to stay the same and the files
that hold a copy; this script fails when two copies differ, and prints the difference.

What counts as "the same":
- Python (`kind = "python"`, the default): the definition named `name` (a function, a class, or a module-level
  assignment). Comments, blank lines, line breaks and docstrings may differ; the code may not.
- Shell (`kind = "shell"`): the function `name() { ... }`, compared as text with comment lines (lines that start
  with `#`) and indentation removed. A `#` later in a line is kept: it may be inside a quoted string.

A name defined more than once at module level (Python), or a shell function defined twice, fails: the later
definition is the one that runs, so comparing the first would pass a copy that does something else.

A file listed for a helper must exist. A listed file that does not define the helper is an error, unless the
helper has `optional = true` (then the copies that exist are compared; used while pull requests that add copies
are open).
"""
from __future__ import annotations

import argparse
import ast
import difflib
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "tools" / "shared_helpers.toml"


class DefinedTwice(ValueError):
    """A file defines the helper more than once; the last definition wins, so the copies cannot be compared."""


def python_definition(source: str, name: str) -> str | None:
    """The code of `name` in normal form, or None when the file does not define it at module level, or nested once
    in a function. A method of a class is not found by its bare name: name the class."""
    tree = ast.parse(source)
    count = sum(1 for node in tree.body if name in _names_defined(node))
    if count > 1:
        raise DefinedTwice(f"defines {name} {count} times at module level")
    found = _find(tree.body, name)
    if found is None:
        nested = [n for top in tree.body if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef))
                  for n in [_find(top.body, name)] if n is not None]
        found = nested[0] if len(nested) == 1 else None
    if found is None:
        return None
    for node in ast.walk(found):        # docstrings are not code
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(found)


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts))
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _names_defined(node: ast.stmt) -> set[str]:
    """The names a module-level statement (re)defines: a function, a class or an assignment of any kind."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return set().union(*(_target_names(t) for t in node.targets))
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return _target_names(node.target)
    return set()


def _find(body: list[ast.stmt], name: str) -> ast.stmt | None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return node
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node
    return None


def shell_definition(source: str, name: str) -> str | None:
    """The text of the shell function `name`, from `name() {` to the closing brace: on the same line for a one-line
    function, else the first line that is only `}`."""
    lines = source.splitlines()
    n = re.escape(name)
    start = re.compile(rf"\s*(?:{n}\s*\(\s*\)|function\s+{n}(?:\s*\(\s*\))?)\s*\{{")
    starts = [i for i, line in enumerate(lines) if start.match(line)]
    if len(starts) > 1:
        raise DefinedTwice(f"defines the function {name} {len(starts)} times")
    if not starts:
        return None
    i = starts[0]
    if lines[i].rstrip().endswith("}"):
        body = [lines[i]]
    else:
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "}"), None)
        if end is None:
            return None
        body = lines[i:end + 1]
    kept = [b.strip() for b in body if not b.strip().startswith("#")]
    return "\n".join(k for k in kept if k)


def check(helpers: list[dict], root: pathlib.Path = ROOT) -> tuple[list[str], list[str]]:
    """Returns (problems, notes). A problem fails the check; a note is printed."""
    problems, notes = [], []
    for h in helpers:
        name, kind = h["name"], h.get("kind", "python")
        extract = shell_definition if kind == "shell" else python_definition
        copies: dict[str, str] = {}
        for rel in h["files"]:
            path = root / rel
            if not path.is_file():
                problems.append(f"{name}: {rel} is listed in the allowlist and does not exist")
                continue
            try:
                text = extract(path.read_text(), name)
            except SyntaxError as err:
                problems.append(f"{name}: {rel} does not parse: {err}")
                continue
            except DefinedTwice as err:
                problems.append(f"{name}: {rel} {err}; keep one definition")
                continue
            if text is None:
                if h.get("optional"):
                    continue
                problems.append(f"{name}: {rel} is listed in the allowlist and does not define it")
            else:
                copies[rel] = text
        if len(copies) < 2:
            notes.append(f"{name}: copies found in {len(copies)} of {len(h['files'])} listed files, nothing to compare")
            continue
        first, reference = next(iter(copies.items()))
        differing = [rel for rel, text in copies.items() if text != reference]
        for rel in differing:
            diff = "\n".join(difflib.unified_diff(reference.splitlines(), copies[rel].splitlines(),
                                                  first, rel, lineterm="", n=1))
            problems.append(f"{name}: the copy in {rel} differs from the one in {first}\n{diff}")
        if not differing:
            notes.append(f"{name}: {len(copies)} copies, the same")
    return problems, notes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="print the helpers and their files, compare nothing")
    args = p.parse_args()
    helpers = tomllib.loads(ALLOWLIST.read_text())["helper"]
    if args.list:
        for h in helpers:
            print(f"{h['name']} ({h.get('kind', 'python')}): {h['why']}")
            for rel in h["files"]:
                print(f"    {rel}")
        return 0
    problems, notes = check(helpers)
    for line in notes:
        print(line)
    for line in problems:
        print(f"DIFFERS  {line}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s). Make the copies the same again (change every copy, or none), or take "
              f"the helper out of tools/shared_helpers.toml and say why in the pull request.", file=sys.stderr)
        return 1
    few = sum(1 for line in notes if line.endswith("nothing to compare"))
    if few:
        print(f"check_shared: {len(helpers)} helpers; {len(helpers) - few} with every copy the same, "
              f"{few} with fewer than two copies found (not compared)")
    else:
        print(f"check_shared: {len(helpers)} helpers, every copy the same")
    return 0


if __name__ == "__main__":
    sys.exit(main())
