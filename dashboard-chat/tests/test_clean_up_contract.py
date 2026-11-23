"""The clean-up contract (AGENTS.md, "Keep the course constraints"): a bare run lists what it would delete, deletes
nothing and ends with "Nothing deleted. Run again with --yes to delete these."; with --yes it deletes. Each entry
point's main() is run with the function that does the work replaced by a stand-in that records whether it was told
to delete, so nothing reaches AWS."""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[1]
MSG = "Nothing deleted. Run again with --yes to delete these."
# (script, the function main hands the work to, how it is told: "yes" or "dry", arguments before the flag, others
# replaced: a value to return, or "flag" for one that is told the same thing)
ENTRIES = [
    ('clean_up.py', 'clean_up', 'dry', [], {}),
    ('ext-athena-engine/clean_up.py', 'clean_up', 'yes', [], {}),
    ('scripts/lake.py', 'down', 'yes', ['down'], {}),
]


class Done:
    """What the stand-in returns: a Steps-like result with nothing in it, which is also a 0 exit code."""
    removed: list = []
    absent: list = []
    failed: list = []
    unchecked: list = []

    def report(self, *a, **k):
        return 0

    def __int__(self):
        return 0


def load(script: str):
    path = RECIPE / script
    for folder in (path.parent, RECIPE, RECIPE / "scripts", RECIPE / "sim", RECIPE / "src"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(f"contract_{path.parent.name}_{path.stem}".replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def told_to_delete(kind: str, args, kwargs) -> bool:
    name = "yes" if kind == "yes" else "dry_run"
    if name in kwargs:
        value = kwargs[name]
    else:
        value = [a for a in args if isinstance(a, bool)][-1]
    return value if kind == "yes" else not value


def run(monkeypatch, capsys, entry, flag: list[str]):
    script, func, kind, before, extra = entry
    module = load(script)
    calls = []

    def stand_in(*args, **kwargs):
        calls.append(told_to_delete(kind, args, kwargs))
        return Done()

    monkeypatch.setattr(module, func, stand_in)
    for name, value in extra.items():
        if value == "flag":
            monkeypatch.setattr(module, name, lambda *a, **k: (calls.append(told_to_delete("dry", a, k)), 0)[1])
        else:
            monkeypatch.setattr(module, name, lambda *a, _v=value, **k: _v)
    argv = before + flag
    params = inspect.signature(module.main).parameters
    try:
        if params and next(iter(params)) in ("argv",):
            module.main(argv)
        else:
            monkeypatch.setattr(sys, "argv", [script] + argv)
            module.main()
    except SystemExit:
        pass
    return calls, capsys.readouterr().out


@pytest.mark.parametrize("entry", ENTRIES, ids=[f"{e[0]} {' '.join(e[3])}".strip() for e in ENTRIES])
def test_a_bare_run_deletes_nothing_and_says_so(monkeypatch, capsys, entry):
    calls, out = run(monkeypatch, capsys, entry, [])
    assert calls and not any(calls), calls
    assert out.rstrip().splitlines()[-1] == MSG, out


@pytest.mark.parametrize("entry", ENTRIES, ids=[f"{e[0]} {' '.join(e[3])}".strip() for e in ENTRIES])
def test_yes_deletes(monkeypatch, capsys, entry):
    calls, out = run(monkeypatch, capsys, entry, ["--yes"])
    assert calls and all(calls), calls
    assert MSG not in out
