"""The run scripts and clean_up.py work end to end against moto, and clean_up removes every table."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_run_refuses_foreign_access(s, ddb):
    lines = load("main/run.py").scenario(s, ddb)
    assert "user-ben read on user-ana's conversation: refused" in lines
    assert "user-ben write on user-ana's conversation: refused" in lines
    assert not any("ALLOWED" in line for line in lines)


def test_cache_run_counts(s, ddb):
    lines = load("ext-answer-cache/cache_run.py").scenario(s, ddb)
    assert lines[-1] == "6 questions, 2 hits, 4 misses, 4 retrievals, 4 model calls"


def test_trace_run_reads_back_four_calls(s, ddb):
    lines = load("ext-traces/trace_run.py").scenario(s, ddb)
    assert len(lines) == 5 and "error" in lines[3]


def run_everything(s, ddb):
    load("main/run.py").scenario(s, ddb)
    load("ext-answer-cache/cache_run.py").scenario(s, ddb)
    load("ext-traces/trace_run.py").scenario(s, ddb)


def test_clean_up_removes_everything(s, ddb):
    c = ddb.meta.client
    assert c.list_tables()["TableNames"] == []  # inventory before the run
    run_everything(s, ddb)
    assert sorted(c.list_tables()["TableNames"]) == sorted(s.all_tables())  # nothing outside all_tables()
    cu = load("clean_up.py")
    planned = cu.clean_up(s, c, dry_run=True)
    assert sorted(planned.removed) == sorted(f"dynamodb table {t}" for t in s.all_tables())
    assert sorted(c.list_tables()["TableNames"]) == sorted(s.all_tables())  # the dry run deleted nothing
    done = cu.clean_up(s, c)
    assert len(done.removed) == 4 and not done.failed
    assert c.list_tables()["TableNames"] == []  # inventory after clean-up equals the one before the run


def test_second_run_and_run_after_clean_up(s, ddb):
    first = load("main/run.py").scenario(s, ddb)
    second = load("main/run.py").scenario(s, ddb)
    assert first[0].startswith("tables created: test-chat-conversations")
    assert second[0] == "tables created: none (already there)" and second[1:] == first[1:]
    cu = load("clean_up.py")
    cu.clean_up(s, ddb.meta.client)
    again = cu.clean_up(s, ddb.meta.client)  # clean-up twice: everything is absent, nothing fails
    assert again.removed == [] and len(again.absent) == 4 and not again.failed
    run_everything(s, ddb)
    assert sorted(ddb.meta.client.list_tables()["TableNames"]) == sorted(s.all_tables())


class StillDeleting:
    """A DynamoDB client where the named tables are being deleted, as in the minute after DeleteTable.

    moto deletes a table at once, so the DELETING state is played here; every other call goes to moto."""

    def __init__(self, real, deleting):
        self.real, self.deleting, self.waited, self.deletes = real, set(deleting), [], []
        self.exceptions = real.exceptions

    def describe_table(self, TableName):
        if TableName in self.deleting:
            return {"Table": {"TableName": TableName, "TableStatus": "DELETING"}}
        return self.real.describe_table(TableName=TableName)

    def delete_table(self, TableName):
        self.deletes.append(TableName)
        if TableName in self.deleting:
            raise self.real.exceptions.ResourceInUseException(
                {"Error": {"Code": "ResourceInUseException", "Message": "Table is being deleted"}}, "DeleteTable")
        return self.real.delete_table(TableName=TableName)

    def get_waiter(self, name):
        outer = self

        class Waiter:
            def wait(self, TableName):
                outer.waited.append((name, TableName))
                if name == "table_not_exists":
                    outer.deleting.discard(TableName)  # the delete finished while we waited

        return Waiter() if name == "table_not_exists" else self.real.get_waiter(name)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_run_right_after_clean_up_waits_for_the_deleting_table(s, ddb):
    from tables import ensure_tables

    c = StillDeleting(ddb.meta.client, [s.history_table])
    created = ensure_tables(s, ["conversations", "history"], c)
    assert ("table_not_exists", s.history_table) in c.waited
    assert created == [s.conversations_table, s.history_table]  # the deleting table was created again
    assert ddb.meta.client.describe_table(TableName=s.history_table)["Table"]["TableStatus"] == "ACTIVE"


def test_clean_up_twice_in_a_row_skips_a_deleting_table_and_goes_on(s, ddb):
    run_everything(s, ddb)
    c = StillDeleting(ddb.meta.client, [s.conversations_table])
    steps = load("clean_up.py").clean_up(s, c)
    assert s.conversations_table not in c.deletes and not steps.failed
    assert steps.absent == [f"dynamodb table {s.conversations_table} (already being deleted)"]
    assert len(steps.removed) == 3  # the tables after the deleting one are still removed


def test_clean_up_reports_a_refused_delete_and_still_tries_the_rest(s, ddb, monkeypatch, capsys):
    import pytest
    from botocore.exceptions import ClientError

    run_everything(s, ddb)
    cu = load("clean_up.py")
    c = ddb.meta.client
    real_delete = c.delete_table

    def refuse_one(TableName):
        if TableName == s.conversations_table:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DeleteTable")
        return real_delete(TableName=TableName)

    monkeypatch.setattr(c, "delete_table", refuse_one)
    steps = cu.clean_up(s, c)
    assert len(steps.removed) == 3 and len(steps.failed) == 1 and "AccessDeniedException" in steps.failed[0]
    assert c.list_tables()["TableNames"] == [s.conversations_table]
    monkeypatch.setattr(cu.boto3, "client", lambda *a, **k: c)
    monkeypatch.setattr("sys.argv", ["clean_up.py", "--yes"])
    with pytest.raises(SystemExit) as e:
        cu.main()
    assert e.value.code == 1 and "FAILED" in capsys.readouterr().err
