"""clean_up.py against a fake OpenSearch Serverless that holds what a run leaves (moto has no collection groups
or access policies): nothing is left, a second run is quiet, and a refused call does not stop the other steps."""

import pytest
from botocore.exceptions import ClientError

import clean_up as cu
from main.config import Settings

NAME = "test-products"


def _error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} from the fake"}}, op)


class FakeAoss:
    """One collection, its group and three policies, plus a teammate's policies that must stay. `refuse` names
    calls that answer AccessDeniedException."""

    def __init__(self, refuse=()):
        self.collections = {NAME: "c1"}
        self.groups = {NAME: "g1"}
        self.policies = {(NAME, "data"), (NAME, "network"), (NAME, "encryption"),
                         ("ana-products", "data"), ("ana-products", "network"), ("ana-products", "encryption")}
        self.refuse, self.calls = set(refuse), []

    def inventory(self) -> set:
        """Everything this prefix owns."""
        return ({("collection", n) for n in self.collections} | {("group", n) for n in self.groups}
                | {p for p in self.policies if p[0] == NAME})

    def _call(self, op: str, *detail):
        if op in self.refuse:
            raise _error("AccessDeniedException", op)
        if op.startswith("delete"):
            self.calls.append((op, *detail))

    deleting = 0          # with n > 0: the collection reports DELETING for n more look-ups, then is gone

    def batch_get_collection(self, names):
        self._call("batch_get_collection")
        if self.deleting:
            self.deleting -= 1
            if not self.deleting:
                self.collections = {}
            return {"collectionDetails": [{"id": "c1", "name": n, "status": "DELETING"} for n in names]}
        return {"collectionDetails": [{"id": self.collections[n], "name": n} for n in names if n in self.collections]}

    def delete_collection(self, id):
        self._call("delete_collection", id)
        self.collections = {n: i for n, i in self.collections.items() if i != id}

    def batch_get_collection_group(self, names):
        self._call("batch_get_collection_group")
        return {"collectionGroupDetails": [{"id": self.groups[n], "name": n} for n in names if n in self.groups]}

    def delete_collection_group(self, id):
        self._call("delete_collection_group", id)
        if self.collections:
            raise _error("ConflictException", "delete_collection_group")      # a group with a collection stays
        self.groups = {n: i for n, i in self.groups.items() if i != id}

    def _get(self, op, name, type):
        self._call(op)
        if (name, type) not in self.policies:
            raise _error("ResourceNotFoundException", op)
        return {}

    def get_access_policy(self, name, type):
        return self._get("get_access_policy", name, type)

    def get_security_policy(self, name, type):
        return self._get("get_security_policy", name, type)

    def delete_access_policy(self, name, type):
        self._call("delete_access_policy", name, type)
        self.policies.discard((name, type))

    def delete_security_policy(self, name, type):
        self._call("delete_security_policy", name, type)
        self.policies.discard((name, type))


def test_clean_up_leaves_nothing_of_this_prefix_and_nothing_else_is_touched():
    aoss = FakeAoss()
    assert len(aoss.inventory()) == 5
    steps = cu.clean_up(Settings(), aoss=aoss, wait_s=1)
    assert aoss.inventory() == set() and steps.failed == [] and len(steps.removed) == 5
    assert [c[0] for c in aoss.calls] == ["delete_collection", "delete_collection_group", "delete_access_policy",
                                          "delete_security_policy", "delete_security_policy"]
    assert {p for p in aoss.policies} == {("ana-products", t) for t in ("data", "network", "encryption")}


def test_dry_run_lists_the_same_five_and_deletes_nothing():
    aoss = FakeAoss()
    steps = cu.clean_up(Settings(), dry_run=True, aoss=aoss)
    assert len(steps.removed) == 5 and aoss.calls == [] and len(aoss.inventory()) == 5


def test_a_second_run_reports_everything_absent_and_exits_zero():
    aoss = FakeAoss()
    cu.clean_up(Settings(), aoss=aoss, wait_s=1)
    again = cu.clean_up(Settings(), aoss=aoss, wait_s=1)
    assert again.removed == [] and again.failed == [] and len(again.absent) == 5
    assert again.report(log=lambda line: None) == 0


def test_a_refused_call_is_reported_and_the_other_steps_still_run():
    """One AccessDenied must not leave the rest."""
    aoss = FakeAoss(refuse={"delete_collection_group", "get_access_policy"})
    steps = cu.clean_up(Settings(), aoss=aoss, wait_s=1)
    assert aoss.inventory() == {("group", NAME), (NAME, "data")}          # only what was refused is left
    assert len(steps.failed) == 2 and all("AccessDeniedException" in f for f in steps.failed)
    lines = []
    assert steps.report(log=lines.append) == 1
    assert sum(line.startswith("FAILED") for line in lines) == 2
    assert sum(line.startswith("removed") for line in lines) == 3


def test_a_collection_that_does_not_go_is_a_failure_and_the_policies_are_still_tried(monkeypatch):
    aoss = FakeAoss()
    aoss.delete_collection = lambda id: None                              # accepted, and never finishes
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    steps = cu.clean_up(Settings(), aoss=aoss, wait_s=0)
    assert any("TimeoutError" in f for f in steps.failed)
    assert any("collection group" in f and "ConflictException" in f for f in steps.failed)
    assert f"data access policy {NAME}" in steps.removed


def test_the_command_exits_non_zero_when_a_step_failed(monkeypatch, capsys):
    aoss = FakeAoss(refuse={"batch_get_collection"})
    monkeypatch.setattr(cu.boto3, "client", lambda *a, **kw: aoss)
    monkeypatch.setattr("sys.argv", ["clean_up.py", "--yes"])
    with pytest.raises(SystemExit) as done:
        cu.main()
    out = capsys.readouterr().out
    assert done.value.code == 1 and "FAILED   collection test-products: AccessDeniedException" in out
    assert "3 resources removed, 2 failed" in out and "run clean_up.py again" in out


def test_a_collection_already_being_deleted_is_absent_and_waited_out(monkeypatch):
    """An earlier clean-up asked for the delete and AWS has not finished: no second delete (it would be refused),
    and the group is deleted only once the collection is gone."""
    aoss = FakeAoss()
    aoss.deleting = 3
    monkeypatch.setattr(cu.time, "sleep", lambda s: None)
    steps = cu.clean_up(Settings(), aoss=aoss, wait_s=5)
    assert steps.failed == [] and f"collection {NAME} (already being deleted)" in steps.absent
    assert ("delete_collection", "c1") not in aoss.calls and f"collection group {NAME}" in steps.removed
    assert aoss.inventory() == set()
