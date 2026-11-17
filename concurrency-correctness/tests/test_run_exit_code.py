"""A run whose own clean-up leaves something exits 1 after the report; a run that failed keeps its own error.

No AWS: the session and the scenario are stand-ins, and clean_up is a fake that reports one failed step.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest

HERE = Path(__file__).resolve().parent.parent


def load(name, rel):
    # by path: main/run.py and ext-idempotent-retry/run.py are both "run"
    spec = importlib.util.spec_from_file_location(name, HERE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_clean_up(failed):
    def clean(session, prefix):
        for f in failed:
            print(f"FAILED   {f}")
        return SimpleNamespace(failed=list(failed))
    return SimpleNamespace(clean=clean)


def race_run(monkeypatch, broken=False):
    mod = load("cc_main_run", "main/run.py")
    rounds = {"rounds": 1, "one_winner": 1, "both_told_yes": 0, "winner_is_stored": 1}
    monkeypatch.setattr(mod, "run_both", MagicMock(return_value={"unsafe": rounds, "safe": rounds}))
    monkeypatch.setattr(mod, "show_one", lambda make, table: "example")
    if broken:
        mod.run_both.side_effect = RuntimeError("table refused")
    return lambda: mod.main(["--target", "aws", "--rounds", "1"])


def retry_run(monkeypatch, broken=False):
    mod = load("cc_retry_run", "ext-idempotent-retry/run.py")
    monkeypatch.setattr(mod, "scenario", MagicMock(return_value={"naive": 2, "request id": 1}))
    if broken:
        mod.scenario.side_effect = RuntimeError("table refused")
    return lambda: mod.main(["--target", "aws"])


RUNS = [race_run, retry_run]


@pytest.fixture(autouse=True)
def no_session(monkeypatch):
    monkeypatch.setattr(boto3, "Session", MagicMock())       # every client is a stand-in: no call leaves the process


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_whose_clean_up_left_something_exits_1(make, monkeypatch, capsys):
    run = make(monkeypatch)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up(["table recipe-test-race-trips: AccessDenied"]))
    with pytest.raises(SystemExit) as stop:
        run()
    assert stop.value.code == 1
    assert "FAILED   table recipe-test-race-trips" in capsys.readouterr().out


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_whose_clean_up_worked_returns(make, monkeypatch):
    run = make(monkeypatch)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up([]))
    run()


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_that_failed_keeps_its_own_error(make, monkeypatch):
    run = make(monkeypatch, broken=True)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up(["table recipe-test-race-trips: AccessDenied"]))
    with pytest.raises(RuntimeError, match="table refused"):
        run()
