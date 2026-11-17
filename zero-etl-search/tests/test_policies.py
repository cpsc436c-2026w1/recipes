"""Every policy names this collection, this index and these principals: no `*` resources, and the function may only
write documents (SEC03-BP02; AWS docs, OpenSearch Serverless data access policies)."""

import json

from main import infra
from main.config import Settings

ROLE = "arn:aws:iam::111122223333:role/test-search-indexer-role"
ME = "arn:aws:iam::111122223333:role/me"


def test_data_access_is_scoped_to_the_collection_the_index_and_the_two_principals():
    s = Settings()
    p = infra.policies(s.collection, s.index, writers=[ROLE], readers=[ME])
    resources = [r for st in p["data"] for rule in st["Rules"] for r in rule["Resource"]]
    assert "*" not in "".join(resources), resources
    assert set(resources) == {f"index/{s.collection}/{s.index}", f"collection/{s.collection}"}
    by_principal = {tuple(st["Principal"]): [perm for rule in st["Rules"] for perm in rule["Permission"]]
                    for st in p["data"]}
    assert by_principal[(ROLE,)] == ["aoss:WriteDocument"]          # the function cannot read, create or delete
    assert "aoss:WriteDocument" not in by_principal[(ME,)] and "aoss:ReadDocument" in by_principal[(ME,)]
    assert not any("aoss:*" in perms for perms in by_principal.values())
    assert "aoss:DeleteIndex" in by_principal[(ME,)]                  # main/migrate.py drop, after a move
    for kind in ("encryption", "network"):
        text = json.dumps(p[kind])
        assert f"collection/{s.collection}" in text and "*" not in text


def test_the_function_role_is_scoped_to_its_stream_queue_logs_and_collection():
    s = Settings()
    stream = "arn:aws:dynamodb:ca-central-1:111122223333:table/test-items/stream/2026-11-12T00:00:00.000"
    queue = "arn:aws:sqs:ca-central-1:111122223333:test-search-indexer-failed"
    coll = "arn:aws:aoss:ca-central-1:111122223333:collection/abcdefgh1234"
    st = infra.indexer_statements(s, "111122223333", stream, queue, coll)
    resources = {st_["Resource"] for st_ in st}
    assert resources == {stream, queue, coll, f"arn:aws:logs:ca-central-1:111122223333:log-group:{s.log_group}:*"}
    actions = {a for st_ in st for a in st_["Action"]}
    assert not any(a.endswith(":*") for a in actions)
    assert {"aoss:APIAccessAll", "sqs:SendMessage", "dynamodb:GetRecords"} <= actions


def test_the_collection_limits_are_the_smallest_the_api_takes():
    """NextGen capacity values: 0, 2, 4, 8, 16, then multiples of 16 (AWS docs, collection group capacity limits)."""
    assert infra.MIN_OCU == 0 and infra.MAX_OCU == 2


def test_the_mapping_retries_are_bounded_and_the_function_has_time_for_its_own():
    from main import handler

    assert 1 <= infra.MAX_RETRIES <= 10 and 1 <= infra.BATCH_SIZE <= 100
    assert infra.MAX_RECORD_AGE_S <= 3600                               # the cap on all the tries
    assert sum(handler.BACKOFF_S) + handler.SAFETY_S < infra.FUNCTION_TIMEOUT_S
    assert infra.MAPPING_SETTINGS["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert infra.MAPPING_SETTINGS["BisectBatchOnFunctionError"] is True


TEAMMATE = "arn:aws:iam::111122223333:role/teammate"


class PolicyStore:
    """OpenSearch Serverless data access policies, as create/get/update see them."""

    class exceptions:  # noqa: N801
        class ConflictException(Exception):
            pass

    def __init__(self):
        self.policy, self.version = None, 0

    def create_access_policy(self, name, type, policy):
        if not json.loads(policy):            # checked before the name, as the service did on 2026-11-13
            raise ValueError("Policy json is invalid, error: [$: there must be a minimum of 1 items in the array]")
        if self.policy is not None:
            raise self.exceptions.ConflictException()
        self.policy, self.version = json.loads(policy), 1

    def get_access_policy(self, name, type):
        return {"accessPolicyDetail": {"policy": self.policy, "policyVersion": str(self.version)}}

    def update_access_policy(self, name, type, policy, policyVersion):
        assert policyVersion == str(self.version)
        self.policy, self.version = json.loads(policy), self.version + 1


def _grants(policy):
    return {(st["Description"], tuple(sorted(st["Principal"])), r["ResourceType"], tuple(sorted(r["Resource"])))
            for st in policy for r in st["Rules"]}


def test_a_teammates_run_adds_their_role_and_keeps_yours():
    s, store = Settings(), PolicyStore()
    infra.put_data_policy(store, s.collection, infra.policies(s.collection, s.index, [ROLE], [ME])["data"])
    infra.put_data_policy(store, s.collection, infra.policies(s.collection, s.index, [ROLE], [TEAMMATE])["data"])
    owner = next(st for st in store.policy if st["Description"] == "owner creates and reads")
    assert owner["Principal"] == [ME, TEAMMATE]
    again = store.version
    infra.put_data_policy(store, s.collection, infra.policies(s.collection, s.index, [ROLE], [ME])["data"])
    assert store.version == again                                      # nothing new: no update call


class Lam:
    """The deployed function's settings, as get_function_configuration returns them."""

    def __init__(self, index="items-v2", role=ROLE):
        self.index, self.role = index, role

    def get_function_configuration(self, FunctionName):
        return {"Role": self.role, "Environment": {"Variables": {"INDEX": self.index}}}


class Client:
    def __init__(self):
        self.deleted = []
        self.indices = self

    def exists(self, index):
        return True

    def delete(self, index):
        self.deleted.append(index)


def test_a_new_index_version_is_added_beside_the_old_one_and_drop_takes_the_old_one_out():
    from main import migrate

    s1, s2, store = Settings(index_version=1), Settings(index_version=2), PolicyStore()
    infra.put_data_policy(store, s1.collection, infra.policies(s1.collection, s1.index, [ROLE], [ME])["data"])
    infra.put_data_policy(store, s2.collection, infra.policies(s2.collection, s2.index, [ROLE], [ME])["data"])
    resources = {r for st in store.policy for rule in st["Rules"] for r in rule["Resource"]}
    assert {f"index/{s1.collection}/items-v1", f"index/{s1.collection}/items-v2"} <= resources   # app keeps v1
    client = Client()
    assert "items-v1 deleted" in migrate.drop(s1, "items-v1", client=client, aoss=store, lam=Lam("items-v2"))
    resources = {r for st in store.policy for rule in st["Rules"] for r in rule["Resource"]}
    assert f"index/{s1.collection}/items-v1" not in resources and f"index/{s1.collection}/items-v2" in resources
    assert client.deleted == ["items-v1"] and "*" not in json.dumps(store.policy)


def test_drop_refuses_the_index_the_deployed_function_writes_to_whatever_index_version_says():
    """Without INDEX_VERSION=2 in your shell, Settings says items-v1; the function writes to items-v2."""
    import pytest

    from main import migrate

    client = Client()
    with pytest.raises(SystemExit, match="writes to now"):
        migrate.drop(Settings(), "items-v2", client=client, aoss=PolicyStore(), lam=Lam("items-v2"))
    assert client.deleted == []


def test_drop_principal_takes_a_teammate_out_and_refuses_you_and_the_function():
    import pytest

    from main import migrate

    s, store = Settings(), PolicyStore()
    infra.put_data_policy(store, s.collection, infra.policies(s.collection, s.index, [ROLE], [ME])["data"])
    infra.put_data_policy(store, s.collection, infra.policies(s.collection, s.index, [ROLE], [TEAMMATE])["data"])
    assert "taken out" in migrate.drop_principal(s, TEAMMATE, aoss=store, lam=Lam(), me=ME)
    assert TEAMMATE not in json.dumps(store.policy) and ME in json.dumps(store.policy)
    for arn in (ME, ROLE):
        with pytest.raises(SystemExit, match="lock you or the function out"):
            migrate.drop_principal(s, arn, aoss=store, lam=Lam(), me=ME)
    assert "is not in" in migrate.drop_principal(s, TEAMMATE, aoss=store, lam=Lam(), me=ME)


def test_a_backfill_goes_through_the_stream_and_cannot_overwrite_a_newer_change():
    """The backfill sets one attribute on every item, so each item's current state reaches the function through
    the stream in order. A change made after the backfill read the table still wins, and a deleted item stays
    deleted."""
    from main import migrate
    from main.envs import LocalEnv

    env = LocalEnv(Settings())
    try:
        env.index.docs.clear()                                          # a new, empty index
        table, real_update = env.table, env.table.update_item
        calls = []

        class Racing:                                                   # changes land while the backfill runs
            meta = table.meta

            def scan(self, **kw):
                return table.scan(**kw)

            def update_item(self, **kw):
                calls.append(kw["Key"]["id"])
                if len(calls) == 1:
                    real_update(Key={"id": "I05"}, UpdateExpression="SET #n = :n",
                                ExpressionAttributeNames={"#n": "name"}, ExpressionAttributeValues={":n": "newer"})
                    table.delete_item(Key={"id": "I06"})
                return real_update(**kw)

        out = migrate.backfill(Racing(), "items-v1")
        assert out == {"items": 7, "gone meanwhile": 1}
        env.deliver()
        assert env.count() == 7 and env.index.get("I05")["name"] == "newer" and env.index.get("I06") is None
        assert table.get_item(Key={"id": "I06"}).get("Item") is None    # not brought back
    finally:
        env.close()


def test_the_probe_is_retried_until_the_function_can_write():
    import io

    answers = [{"ok": False, "detail": "HTTP 403"}, {"ok": False, "detail": "HTTP 503"}, {"ok": True, "detail": "[201, 200]"}]

    class Lam:
        def invoke(self, FunctionName, Payload):
            assert json.loads(Payload) == {"probe": True}
            return {"Payload": io.BytesIO(json.dumps(answers.pop(0)).encode())}

    lines = []
    assert infra.wait_until_function_can_write(Lam(), Settings(), every_s=0, log=lines.append) == "[201, 200]"
    assert len(lines) == 2 and "HTTP 403" in lines[0]


def test_index_creation_waits_while_the_new_data_access_policy_spreads(monkeypatch):
    from opensearchpy.exceptions import AuthenticationException

    answers = [AuthenticationException(401, ""), AuthenticationException(401, ""), False]
    created = []

    class Indices:
        def exists(self, index):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        def create(self, index, body):
            created.append(index)

        def get_mapping(self, index):
            return {index: {"mappings": {"_meta": {"index_version": 1}}}}

    class Client:
        indices = Indices()

    monkeypatch.setattr(infra.time, "sleep", lambda s: None)
    lines = []
    assert infra.create_index(Client(), "items-v1", {}, every_s=0, log=lines.append) == {"index_version": 1}
    assert created == ["items-v1"] and len(lines) == 3 and "refused (401)" in lines[0]
    assert lines[-1].startswith("collection let you in")
