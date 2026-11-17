"""The OpenSearch path without a cluster: request bodies, the search class over a fake client, infra and clean-up
calls (moto covers the security policies and the collection; a small fake covers what moto does not)."""

import json

from main.config import Settings, load_products
from main.embed import StubEmbedder
from main.opensearch_store import (OpenSearchSearch, access_policy, index_body, keyword_query, knn_query,
                                   network_policy, principal_arn)


def test_bodies():
    assert index_body(1024)["mappings"]["properties"]["embedding"] == {
        "type": "knn_vector", "dimension": 1024, "space_type": "cosinesimil"}
    kq = keyword_query("sofa", 20, [{"term": {"in_stock": True}}])
    assert kq["query"]["bool"]["filter"] == [{"term": {"in_stock": True}}] and kq["size"] == 20
    nq = knn_query([0.1, 0.2], 20, [{"term": {"in_stock": True}}])
    assert nq["query"]["knn"]["embedding"]["filter"] == {"bool": {"filter": [{"term": {"in_stock": True}}]}}
    assert network_policy("test-products")[0]["AllowFromPublic"] is True
    assert access_policy("test-products", "arn:aws:iam::111122223333:role/r")[0]["Principal"] == [
        "arn:aws:iam::111122223333:role/r"]


class FakeOpenSearch:
    """Answers search requests from fixed hit lists, keyed by the query type."""

    def __init__(self):
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        if "knn" in json.dumps(body["query"]):
            hits = [("P09", 0.9), ("P10", 0.8)]
        else:
            hits = [("P10", 12.0), ("P11", 3.0)]
        return {"hits": {"hits": [{"_id": i, "_score": s} for i, s in hits]}}


def test_hybrid_over_a_fake_cluster():
    s = OpenSearchSearch(FakeOpenSearch(), "products", StubEmbedder(), load_products())
    fused = [i for i, _ in s.hybrid("sofa", 3)]
    assert fused == ["P10", "P09", "P11"]  # P10 is in both lists
    assert len(s.client.bodies) == 2


class FakeSts:
    def __init__(self, arn):
        self.arn = arn

    def get_caller_identity(self):
        return {"Arn": self.arn}


class FakeIam:
    def get_role(self, RoleName):
        return {"Role": {"Arn": f"arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/{RoleName}"}}


def test_principal_keeps_the_role_path():
    sts = FakeSts("arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Team_abc/student")
    assert principal_arn(sts, FakeIam()).endswith("/sso.amazonaws.com/AWSReservedSSO_Team_abc")
    assert principal_arn(FakeSts("arn:aws:iam::111122223333:user/u"), FakeIam()) == "arn:aws:iam::111122223333:user/u"


def test_security_policies_in_moto():
    """moto 5.2 stores encryption policies. It has no collection groups or access policies, reads a network policy
    as an encryption policy and wants a KMS key for a collection, so those are checked with FakeAoss above."""
    import boto3
    from moto import mock_aws

    from main.opensearch_store import encryption_policy

    with mock_aws():
        aoss = boto3.client("opensearchserverless", region_name="ca-central-1")
        name = Settings().collection
        aoss.create_security_policy(name=name, type="encryption", policy=json.dumps(encryption_policy(name)))
        names = [p["name"] for p in aoss.list_security_policies(type="encryption")["securityPolicySummaries"]]
        assert names == [name]
        stored = aoss.get_security_policy(name=name, type="encryption")["securityPolicyDetail"]["policy"]
        assert stored == encryption_policy(name)  # boto3 returns the policy document parsed
