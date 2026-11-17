"""Creating the collection, against a fake that answers as OpenSearch Serverless did on 2026-11-12: the first run,
a second run, and a run right after clean_up.py."""

from botocore.exceptions import ClientError

import main.opensearch_store as store
from main.config import Settings


class _Conflict(Exception):
    pass


class NextGenAoss:
    """A NextGen group refuses standbyReplicas DISABLED (output/main-opensearch-refused.txt); a second create of
    anything answers ConflictException; a collection that is being deleted still answers ConflictException to a
    create, and is gone a few polls later."""

    class exceptions:  # noqa: N801 - the name boto3 clients use
        ConflictException = _Conflict

    def __init__(self):
        self.policies, self.groups, self.collections, self.polls = set(), {}, {}, 0

    def create_security_policy(self, name, type, policy):
        self._once((name, type))

    def create_access_policy(self, name, type, policy):
        self._once((name, type))

    def _once(self, key):
        if key in self.policies:
            raise _Conflict()
        self.policies.add(key)

    def create_collection_group(self, name, generation, standbyReplicas, capacityLimits):
        if generation == "NEXTGEN" and standbyReplicas != "ENABLED":
            raise ClientError({"Error": {"Code": "ValidationException", "Message":
                               "StandbyReplicas cannot be set to DISABLED for NEXTGEN collection groups."}},
                              "CreateCollectionGroup")
        if name in self.groups:
            raise _Conflict()
        self.groups[name] = standbyReplicas

    def create_collection(self, name, type, standbyReplicas, collectionGroupName, tags):
        assert standbyReplicas == self.groups[collectionGroupName]
        if name in self.collections:
            raise _Conflict()
        self.collections[name] = "ACTIVE"

    def batch_get_collection(self, names):
        self.polls += 1
        name = names[0]
        if self.collections.get(name) == "DELETING" and self.polls > 2:
            del self.collections[name]            # the delete finishes while the next run waits
        if name not in self.collections:
            return {"collectionDetails": []}
        return {"collectionDetails": [{"id": "c1", "name": name, "status": self.collections[name],
                                       "collectionEndpoint": f"https://c1.{name}.example"}]}


class _Sts:
    def get_caller_identity(self):
        return {"Arn": "arn:aws:iam::111122223333:user/u"}


def _create(aoss, monkeypatch):
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    return store.create_collection(Settings(), aoss=aoss, sts=_Sts(), iam=None, wait_s=5)


def test_a_nextgen_group_accepts_the_standby_setting_we_send(monkeypatch):
    aoss = NextGenAoss()
    assert _create(aoss, monkeypatch) == "https://c1.test-products.example"
    assert aoss.groups == {"test-products": "ENABLED"} and len(aoss.policies) == 3


def test_a_second_run_finds_everything_and_returns_the_same_endpoint(monkeypatch):
    aoss = NextGenAoss()
    assert _create(aoss, monkeypatch) == _create(aoss, monkeypatch)
    assert len(aoss.collections) == 1 and len(aoss.groups) == 1


def test_a_run_right_after_clean_up_waits_for_the_deleting_collection(monkeypatch):
    aoss = NextGenAoss()
    _create(aoss, monkeypatch)
    aoss.collections["test-products"] = "DELETING"      # clean_up.py asked for it; AWS has not finished
    aoss.polls = 0
    assert _create(aoss, monkeypatch) == "https://c1.test-products.example"
    assert aoss.collections == {"test-products": "ACTIVE"}


def test_a_group_that_vanishes_after_already_exists_is_made_again(monkeypatch):
    """The group an earlier clean-up was deleting answers ConflictException to a create and is then gone; the
    collection create names it and is refused. The group is made again and the collection create retried."""
    aoss = NextGenAoss()
    aoss.groups["test-products"] = "ENABLED"            # still listed while being deleted
    real_create = aoss.create_collection
    refused = []

    def create_collection(**kw):
        if not refused:
            refused.append(1)
            aoss.groups.clear()                          # the delete finishes
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no such group"}},
                              "CreateCollection")
        return real_create(**kw)

    aoss.create_collection = create_collection
    assert _create(aoss, monkeypatch) == "https://c1.test-products.example"
    assert aoss.groups == {"test-products": "ENABLED"} and refused == [1]


def test_only_a_refusal_about_the_group_is_retried():
    err = lambda code, msg: ClientError({"Error": {"Code": code, "Message": msg}}, "CreateCollection")  # noqa: E731
    assert store.group_gone(err("ResourceNotFoundException", "no such group"), "test-products")
    assert store.group_gone(err("ValidationException", "Collection group test-products not found"), "test-products")
    assert not store.group_gone(err("ValidationException", "Invalid collection name"), "test-products")
