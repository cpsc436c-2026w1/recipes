"""The production path: one Amazon OpenSearch Serverless collection holds both indexes in one index.

One document per product: text fields for BM25 (name, description), keyword and number fields for filters
(category, brand, price_cad, in_stock), and a knn_vector field for the embedding. Two queries per search, a `match`
and a `knn`, fused by the same rrf() as on the laptop, so the eval compares like with like (README, "Decisions").

Written from the AWS and OpenSearch documentation (links in README, "Run it") and checked against the boto3
service model. One sandbox run (2026-11-12, output/main-opensearch-refused.txt) created the three policies and was
refused at the collection group; the whole path ran in a sandbox on 2026-11-14 (output/main-opensearch.txt).
Created resources, all named from PREFIX (clean_up.py removes them):

  collection group   {PREFIX}-products   NextGen generation, minimum 0 OCU: scales to zero after 10 idle minutes
  collection         {PREFIX}-products   type VECTORSEARCH, in that group
  security policies  {PREFIX}-products   encryption (AWS-owned key) and network (public endpoint, IAM auth)
  access policy      {PREFIX}-products   index read and write for the identity that runs this script
"""

from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from main.config import EMBEDDING_DIM, Settings, doc_text
from main.fusion import rrf
from main.search import POOL

MAX_OCU = 2  # ours: the cap on what the group may scale to; a demo of 30 products needs the minimum
STANDBY_REPLICAS = "ENABLED"  # the only value a NextGen collection group accepts (measured, 2026-11-12)


def encryption_policy(name: str) -> dict:
    return {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}], "AWSOwnedKey": True}


def network_policy(name: str) -> list:
    # Public endpoint: every request still needs a SigV4 signature from an identity in the access policy.
    # A team's production service would use a VPC endpoint instead (GENSEC01-BP02).
    return [{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}], "AllowFromPublic": True}]


def access_policy(name: str, principal_arn: str) -> list:
    return [{
        "Rules": [
            {"ResourceType": "index", "Resource": [f"index/{name}/*"],
             "Permission": ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex", "aoss:DeleteIndex",
                            "aoss:ReadDocument", "aoss:WriteDocument"]},
            {"ResourceType": "collection", "Resource": [f"collection/{name}"],
             "Permission": ["aoss:DescribeCollectionItems"]},
        ],
        "Principal": [principal_arn],
    }]


def index_body(dim: int) -> dict:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {"properties": {
            "name": {"type": "text"},
            "description": {"type": "text"},
            "category": {"type": "keyword"},
            "brand": {"type": "keyword"},
            "price_cad": {"type": "float"},
            "in_stock": {"type": "boolean"},
            # NextGen collections choose the engine and mode themselves (no "method" block needed)
            "embedding": {"type": "knn_vector", "dimension": dim, "space_type": "cosinesimil"},
        }},
    }


def keyword_query(query: str, k: int, filters: list | None = None) -> dict:
    match = {"multi_match": {"query": query, "fields": ["name^2", "description", "category", "brand"]}}
    if filters:
        match = {"bool": {"must": [match], "filter": filters}}
    return {"size": k, "_source": False, "query": match}


def knn_query(vector: list[float], k: int, filters: list | None = None) -> dict:
    knn = {"vector": vector, "k": k}
    if filters:
        knn["filter"] = {"bool": {"filter": filters}}  # filtering inside the k-NN search (ext-filters)
    return {"size": k, "_source": False, "query": {"knn": {"embedding": knn}}}


def principal_arn(sts, iam) -> str:
    """The IAM role (or user) behind the current credentials, in the form the access policy wants."""
    arn = sts.get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        return arn
    role = arn.split(":assumed-role/")[1].split("/")[0]
    try:
        return iam.get_role(RoleName=role)["Role"]["Arn"]  # keeps the role's path (SSO roles have one)
    except Exception:
        account = arn.split(":")[4]
        return f"arn:aws:iam::{account}:role/{role}"


def create_collection(s: Settings, aoss=None, sts=None, iam=None, wait_s: int = 900) -> str:
    """Create the policies, the group and the collection; return the collection endpoint (https://...)."""
    import boto3

    aoss = aoss or boto3.client("opensearchserverless", region_name=s.region)
    sts = sts or boto3.client("sts", region_name=s.region)
    iam = iam or boto3.client("iam", region_name=s.region)
    name = s.collection

    def ignore_conflict(fn, **kw):
        try:
            fn(**kw)
        except aoss.exceptions.ConflictException:
            pass  # already there from an earlier run

    ignore_conflict(aoss.create_security_policy, name=name, type="encryption",
                    policy=json.dumps(encryption_policy(name)))
    ignore_conflict(aoss.create_security_policy, name=name, type="network", policy=json.dumps(network_policy(name)))
    ignore_conflict(aoss.create_access_policy, name=name, type="data",
                    policy=json.dumps(access_policy(name, principal_arn(sts, iam))))
    # A NextGen group takes standbyReplicas ENABLED only: DISABLED (one copy, for development) was refused on
    # 2026-11-12 with "StandbyReplicas cannot be set to DISABLED for NEXTGEN collection groups"
    # (output/main-opensearch-refused.txt). The collection carries the same value as its group.
    deadline = time.time() + wait_s

    def create_group():
        ignore_conflict(aoss.create_collection_group, name=name, generation="NEXTGEN",
                        standbyReplicas=STANDBY_REPLICAS,
                        capacityLimits={"minIndexingCapacityInOCU": 0, "minSearchCapacityInOCU": 0,
                                        "maxIndexingCapacityInOCU": MAX_OCU, "maxSearchCapacityInOCU": MAX_OCU})

    create_group()
    # A collection that clean_up.py is still deleting answers "already exists" to a create, and then disappears:
    # wait until it is gone, so the create below makes a new one.
    while _status(aoss, name) == "DELETING" and time.time() < deadline:
        time.sleep(15)
    # A group that an earlier clean-up was deleting can answer "already exists" above and then be gone: the
    # collection create then names a group that does not exist. Make the group again and retry until the deadline.
    # (The API reports no status for a group, so "being deleted" cannot be read: CollectionGroupDetail,
    # https://docs.aws.amazon.com/opensearch-service/latest/ServerlessAPIReference/API_CollectionGroupDetail.html)
    while True:
        try:
            ignore_conflict(aoss.create_collection, name=name, type="VECTORSEARCH", standbyReplicas=STANDBY_REPLICAS,
                            collectionGroupName=name, tags=[{"key": "recipe", "value": "hybrid-retrieval-eval"}])
            break
        except ClientError as err:
            if not group_gone(err, name) or time.time() > deadline:
                raise
            time.sleep(15)
            create_group()
    while time.time() < deadline:
        details = aoss.batch_get_collection(names=[name])["collectionDetails"]
        if details and details[0]["status"] == "ACTIVE":
            return details[0]["collectionEndpoint"]
        if details and details[0]["status"] == "FAILED":
            raise RuntimeError(f"collection {name} failed: {details[0]}")
        time.sleep(15)
    raise TimeoutError(f"collection {name} not active after {wait_s} s")


def group_gone(err: ClientError, group: str) -> bool:
    """True when a create_collection refusal says its collection group does not exist: ResourceNotFoundException,
    or a ValidationException whose message names the group. Any other ValidationException is a real mistake."""
    code, message = err.response["Error"]["Code"], err.response["Error"].get("Message", "")
    return code == "ResourceNotFoundException" or (
        code == "ValidationException" and ("collection group" in message.lower() or group in message))


def _status(aoss, name: str) -> str:
    details = aoss.batch_get_collection(names=[name])["collectionDetails"]
    return details[0]["status"] if details else ""


def client_for(endpoint: str, region: str):
    import boto3
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss")  # "es" for a managed domain
    return OpenSearch(hosts=[{"host": endpoint.removeprefix("https://"), "port": 443}], http_auth=auth,
                      use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=60)


class OpenSearchSearch:
    """Same methods as main.search.LocalSearch: keyword, embedding, hybrid; `filters` used by ext-filters."""

    def __init__(self, client, index: str, embedder, products: list[dict]):
        self.client, self.index, self.embedder = client, index, embedder
        self.products = {p["id"]: p for p in products}

    @classmethod
    def create(cls, products: list[dict], embedder, s: Settings) -> "OpenSearchSearch":
        endpoint = create_collection(s)
        client = client_for(endpoint, s.region)
        create_index(client, s.index, index_body(getattr(embedder, "dim", EMBEDDING_DIM)))
        vectors = embedder.embed([doc_text(p) for p in products])
        lines = []
        for p, v in zip(products, vectors):
            lines.append(json.dumps({"index": {"_index": s.index, "_id": p["id"]}}))  # NextGen allows own ids
            lines.append(json.dumps({k: p[k] for k in ("name", "description", "category", "brand", "price_cad",
                                                       "in_stock")} | {"embedding": [float(x) for x in v]}))
        client.bulk(body="\n".join(lines) + "\n")
        _wait_for_count(client, s.index, len(products))
        return cls(client, s.index, embedder, products)

    def _ids(self, body: dict) -> list[tuple[str, float]]:
        hits = self.client.search(index=self.index, body=body)["hits"]["hits"]
        return [(h["_id"], h["_score"]) for h in hits]

    def keyword(self, query: str, k: int = 10, filters: list | None = None):
        return self._ids(keyword_query(query, k, filters))

    def embedding(self, query: str, k: int = 10, filters: list | None = None):
        vector = [float(x) for x in self.embedder.embed([query])[0]]
        return self._ids(knn_query(vector, k, filters))

    def hybrid(self, query: str, k: int = 10, filters: list | None = None):
        return rrf([self.keyword(query, POOL, filters), self.embedding(query, POOL, filters)], top=k)


INDEX_WAIT_S = 300   # ours: five times the 62 s that zero-etl-search's collection refused its creator after ACTIVE
INDEX_EVERY_S = 10


def create_index(client, name: str, body: dict, wait_s: int = INDEX_WAIT_S, every_s: float = INDEX_EVERY_S,
                 log=print) -> None:
    """Create the index, trying again while the new collection refuses: right after a collection is ACTIVE its
    network policy (401) or data access policy (403) may not apply yet, and the AWS docs name no time for it
    (https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html).
    zero-etl-search's sandbox run of 2026-11-13 got 401 for the first 62 s (its output/main-aws.txt)."""
    from opensearchpy.exceptions import AuthenticationException, AuthorizationException

    started = time.time()
    deadline = started + wait_s
    while True:
        try:
            if not client.indices.exists(index=name):
                client.indices.create(index=name, body=body)
            return
        except (AuthenticationException, AuthorizationException) as err:
            if time.time() + every_s > deadline:
                raise
            log(f"{time.time() - started:.0f} s after ACTIVE: refused ({err.status_code}); trying again in "
                f"{every_s:.0f} s")
            time.sleep(every_s)


def _wait_for_count(client, index: str, n: int, wait_s: int = 120) -> None:
    """New documents become searchable after the refresh interval (5 s on NextGen vector collections:
    https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html)."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if client.count(index=index)["count"] >= n:
            return
        time.sleep(5)
    raise TimeoutError(f"index {index} has fewer than {n} documents after {wait_s} s")
