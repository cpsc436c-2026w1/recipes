"""The clean-up contract (AGENTS.md, "Keep the course constraints") for ext-nearby/clean_up.py: a bare run lists what
it would delete, deletes nothing and ends with "Nothing deleted. Run again with --yes to delete these."; with --yes it
deletes. The ledger and DynamoDB are stand-ins. (clean_up.sh: tests/test_clean_up_and_rerun.py.)"""
import importlib.util
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]
MSG = "Nothing deleted. Run again with --yes to delete these."


def load():
    path = RECIPE / "ext-nearby" / "clean_up.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("contract_nearby_clean_up", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Client:
    def __init__(self):
        self.deleted = []

    def delete_table(self, TableName):
        self.deleted.append(TableName)

    def get_waiter(self, name):
        return type("W", (), {"wait": lambda self, **k: None})()


def prepared(monkeypatch):
    m = load()
    table = m.nearby.names()["cells"]
    c = Client()
    monkeypatch.setattr(m, "owned_tables", lambda: [table])
    monkeypatch.setattr(m, "borrowed_drivers", lambda: False)
    monkeypatch.setattr(m, "record", lambda *a, **k: None)
    monkeypatch.setattr(m.nearby, "table_status", lambda client, t: None if t in c.deleted or t != table else "ACTIVE")
    return m, c, table


def test_a_bare_run_deletes_nothing_and_says_so(monkeypatch, capsys):
    m, c, table = prepared(monkeypatch)
    m.main([], c=c)
    out = capsys.readouterr().out
    assert c.deleted == [] and table in out
    assert out.rstrip().splitlines()[-1] == MSG


def test_yes_deletes(monkeypatch, capsys):
    m, c, table = prepared(monkeypatch)
    m.main(["--yes"], c=c)
    assert c.deleted == [table] and MSG not in capsys.readouterr().out
