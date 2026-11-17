"""A run whose own clean-up leaves something exits 1 after the report; a run that failed keeps its own error.

No AWS: the session and every AWS-facing step are stand-ins, and clean_up is a fake that reports one failed step.
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


def main_run(monkeypatch, broken=False):
    mod = load("si_main_run", "main/run.py")
    monkeypatch.setattr(mod, "infra", MagicMock())
    monkeypatch.setattr(mod, "producer", MagicMock())
    monkeypatch.setattr(mod, "lag", MagicMock())
    monkeypatch.setattr(mod, "wait_stored", lambda ddb, table, expected, wait_s: expected)
    if broken:
        mod.infra.deploy_main.side_effect = RuntimeError("deploy refused")
    return lambda: mod.run_aws(2, "recipe-test", rate=0, wait_s=0, keep=False)


def fan_out_run(monkeypatch, broken=False):
    mod = load("si_fan_out_run", "ext-fan-out/run.py")
    monkeypatch.setattr(mod, "infra", MagicMock())
    monkeypatch.setattr(mod, "lag", MagicMock())
    monkeypatch.setattr(mod, "make_topic_and_queues", MagicMock())
    monkeypatch.setattr(mod, "publish", lambda *a, **kw: 2)
    monkeypatch.setattr(mod, "wait_count", lambda *a, **kw: 2)
    monkeypatch.setattr(mod, "queue_depth", lambda *a, **kw: 0)
    if broken:
        mod.make_topic_and_queues.side_effect = RuntimeError("deploy refused")
    return lambda: mod.run_aws(2, 0, "recipe-test", rate=0, wait_s=0, keep=False)


def windowed_run(monkeypatch, broken=False):
    mod = load("si_windowed_run", "ext-windowed-aggregates/run.py")
    monkeypatch.setattr(mod, "infra", MagicMock())
    monkeypatch.setattr(mod, "producer", MagicMock())
    monkeypatch.setattr(mod, "deploy", MagicMock(return_value="uuid"))
    monkeypatch.setattr(mod.windows, "read_windows", lambda ddb, table: ([], 0))
    monkeypatch.setattr(mod, "compare", lambda *a: [])
    if broken:
        mod.deploy.side_effect = RuntimeError("deploy refused")
    return lambda: mod.run_aws("drop", 120, 25, "recipe-test", rate=0, wait_s=0, keep=False)


RUNS = [main_run, fan_out_run, windowed_run]


@pytest.fixture(autouse=True)
def no_session(monkeypatch):
    monkeypatch.setattr(boto3, "Session", MagicMock())       # every client is a stand-in: no call leaves the process


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_whose_clean_up_left_something_exits_1(make, monkeypatch, capsys):
    run = make(monkeypatch)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up(["table recipe-test-trips: AccessDenied"]))
    with pytest.raises(SystemExit) as stop:
        run()
    assert stop.value.code == 1
    assert "FAILED   table recipe-test-trips" in capsys.readouterr().out


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_whose_clean_up_worked_returns(make, monkeypatch):
    run = make(monkeypatch)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up([]))
    run()


@pytest.mark.parametrize("make", RUNS, ids=lambda f: f.__name__)
def test_a_run_that_failed_keeps_its_own_error(make, monkeypatch):
    run = make(monkeypatch, broken=True)
    monkeypatch.setitem(sys.modules, "clean_up", fake_clean_up(["table recipe-test-trips: AccessDenied"]))
    with pytest.raises(RuntimeError, match="deploy refused"):
        run()
