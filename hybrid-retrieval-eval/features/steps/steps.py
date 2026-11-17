"""Steps of the scenarios in features/. They drive the recipe's entry points (main.run.main, clean_up.main,
filters_run.main, routing_eval.main) and its search class; features/support.py holds the stand-ins."""

from __future__ import annotations

import json
import os
import re
import socket
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

from behave import given, then, when

import support
from support import GATEWAY_URL, TOKEN, Aoss, Endpoint, bodies, patch, run, table_rows

from main.config import ROOT, load_products
from main.embed import StubEmbedder
from main.search import LocalSearch


def _outputs():
    return {p.name: p.stat().st_mtime_ns for p in (ROOT / "output").glob("*")}


# ---- given ------------------------------------------------------------------------------------------------------

@given("a catalogue of the products")
def catalogue(context):
    products = [dict(row.as_dict(), price_cad=10.0, in_stock=True) for row in context.table]
    context.embedded = []
    embedder = StubEmbedder()
    real = embedder.embed

    def embed(texts):
        context.embedded += list(texts)
        return real(texts)

    embedder.embed = embed
    context.search = LocalSearch(products, embedder)


@given("the recipe's catalogue")
def recipe_catalogue(context):
    context.search = LocalSearch(load_products(), StubEmbedder())


@given('a catalogue of {n:d} products that all match "{word}"')
def same_products(context, n, word):
    products = [{"id": f"P{i:02d}", "name": f"{word.title()} {i}", "description": f"A {word} for a desk",
                 "category": "lighting", "brand": "Glow", "price_cad": 10.0 + i, "in_stock": True}
                for i in range(1, n + 1)]
    context.search = LocalSearch(products, StubEmbedder())


@given('a labelled query "{query}" whose relevant product is {product}')
def one_query(context, query, product):
    context.queries_file = context.tmp / "queries.jsonl"
    context.queries_file.write_text(json.dumps({"id": "q1", "query": query, "kind": "plain words",
                                                "relevant": [product]}) + "\n")


@given("{n:d} labelled queries, each asked once")
def many_queries(context, n):
    context.queries_file = context.tmp / "queries.jsonl"
    context.queries_file.write_text("".join(json.dumps({"id": f"q{i}", "query": f"query number {i}",
                                                        "kind": "plain words", "relevant": ["P01"]}) + "\n"
                                            for i in range(n)))


@given("GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values")
def gateway_env(context):
    os.environ["GATEWAY_URL"], os.environ["COURSE_MCP_TOKEN"] = GATEWAY_URL, TOKEN


@given("{variable:w} is not set")
def unset(context, variable):
    os.environ.pop(variable, None)


@given("the gateway is a stand-in")
def gateway(context):
    support.use_gateway(context)


@given('the gateway is a stand-in whose answers say "{reply}"')
def gateway_reply(context, reply):
    def answer(body):
        first = re.findall(r"^\[(P\d+)\]", body["messages"][-1]["content"], flags=re.M)[0]
        return reply.replace("[FIRST]", f"[{first}]")

    support.use_gateway(context, reply=answer)


@given('the gateway is a stand-in that refuses the first call with HTTP {status:d} "{code}" and Retry-After "{ra}"')
def gateway_refuses(context, status, code, ra):
    support.use_gateway(context, refusals=[(status, code, ra)])


@given('the gateway is a stand-in that refuses the first {n:d} calls with HTTP {status:d} "{code}" and Retry-After "{ra}"')
def gateway_refuses_n(context, n, status, code, ra):
    support.use_gateway(context, refusals=[(status, code, ra)] * n)


@given('the gateway is a stand-in that refuses the first call with HTTP 429 "{code}" and a Retry-After date {s:d} seconds ahead')
def gateway_refuses_date(context, code, s):
    when_ = datetime.now(timezone.utc) + timedelta(seconds=s)
    support.use_gateway(context, refusals=[(429, code, format_datetime(when_, usegmt=True))])


@given("the gateway is a stand-in that returns one embedding fewer than it was sent")
def gateway_short(context):
    support.use_gateway(context, short_by=1)


def _router_reply(rule):
    from router import ROUTER_SYSTEM

    def answer(body):
        if body["messages"][0]["content"] == ROUTER_SYSTEM:
            return rule(body["messages"][-1]["content"])
        ids = re.findall(r"^\[(P\d+)\]", body["messages"][-1]["content"], flags=re.M)
        return f"Try [{ids[0]}]."

    return answer


@given('the gateway is a stand-in whose router replies "{a}" to "{word}" and "{b}" to anything else')
def gateway_router_rule(context, a, word, b):
    support.use_gateway(context, reply=_router_reply(lambda text: a if word in text else b))


@given('the gateway is a stand-in whose router replies "{reply}" to everything')
def gateway_router_all(context, reply):
    support.use_gateway(context, reply=_router_reply(lambda text: reply))


@given("the gateway is a stand-in whose stronger model spends its tokens on reasoning for {n:d} of the 10 requests")
def gateway_strong_empty(context, n):
    """The router says "simple" to everything, so only the all-strong policy calls the stronger model. Its first n
    answers come back as a reasoning model's do when max_tokens runs out: no text, finish_reason "length"."""
    from main.config import STRONG_MODEL

    inner, seen = _router_reply(lambda text: "simple"), []

    def answer(body):
        if body["model"] == STRONG_MODEL:
            seen.append(body)
            if len(seen) <= n:
                return ("", "length")
        return inner(body)

    support.use_gateway(context, reply=answer)


@then('every answer call to "{model}" asks for max_tokens {n:d}')
def answer_calls_cap(context, model, n):
    from router import ROUTER_SYSTEM

    calls = [b for b in bodies(context) if b["messages"][0]["content"] != ROUTER_SYSTEM and b.get("model") == model
             and "messages" in b]
    assert calls and all(b["max_tokens"] == n for b in calls), sorted({b["max_tokens"] for b in calls})


@then("the all-strong accuracy is the share of correct answers among the {n:d} that were scored")
def strong_accuracy_over_scored(context, n):
    strong = next(p for p in _routing(context) if p["policy"] == "all-strong")
    scored = [r for r in strong["rows"] if r["error"] is None]
    assert len(scored) == n == strong["scored"]
    assert all(r["correct"] is None for r in strong["rows"] if r["error"])
    assert strong["accuracy"] == sum(r["correct"] for r in scored) / n
    row = next(r for r in _table(context, "| policy |") if r[0] == "all-strong")
    assert row[1] == f"{strong['accuracy']:.2f}", row


@then("the {policy} policy has no answer that was not scored")
def policy_all_scored(context, policy):
    p = next(p for p in _routing(context) if p["policy"] == policy)
    assert p["errors"] == [] and p["scored"] == 10


@given("OpenSearch Serverless is a stand-in")
def aoss_stand_in(context):
    from moto import mock_aws

    import main.opensearch_store as store

    m = mock_aws()
    m.start()
    context.add_cleanup(m.stop)
    support.use_aoss(context, Aoss())
    support.use_endpoint(context, Endpoint())
    import clean_up

    support.no_waits(context, store, clean_up)
    regions = context.aoss_regions = []
    real = support.Aoss.answer

    def answer(self, client, operation, p):
        regions.append(client.meta.region_name)
        return real(self, client, operation, p)

    patch(context, support.Aoss, "answer", answer)


@given("everything of an earlier run is still there")
def earlier_run(context):
    import main.run as run_module

    run(context, run_module.main, argv=["--backend", "opensearch", "--out", str(context.tmp)])
    assert context.exit_code == 0, context.printed
    context.aoss.calls.clear()
    context.printed = ""


@given('a teammate\'s collection "{name}" with its group and policies')
def teammate(context, name):
    a = context.aoss
    a.groups[name] = {"id": f"g-{name}", "name": name}
    a.collections[name] = {"id": f"c-{name}", "name": name, "type": "VECTORSEARCH", "group": name,
                           "status": "ACTIVE", "polls": 0, "params": {}}
    for kind in ("data", "network", "encryption"):
        a.policies[(name, kind)] = {}


@given('the collection "{name}" is being deleted')
def being_deleted(context, name):
    a = context.aoss
    a.groups.setdefault(name, {"id": f"g-{name}", "name": name})
    a.collections[name] = {"id": f"c-{name}-old", "name": name, "type": "VECTORSEARCH", "group": name,
                           "status": "DELETING", "polls": 2, "params": {}, "old": True}


@given('the group "{name}" answers "already exists" and is then gone')
def group_vanishes(context, name):
    context.aoss.groups[name] = {"id": f"g-{name}", "name": name, "vanish": True}


@given("the collection never becomes ACTIVE")
def never_active(context):
    context.aoss.collection_never_active = True


@given('OpenSearch Serverless refuses {operation:w} with "{code}"')
def aoss_refuses(context, operation, code):
    context.aoss.refuse[operation] = code


@given("every network connection fails the scenario")
def no_network(context):
    def refuse(self, address):
        raise AssertionError(f"the run opened a network connection to {address}")

    patch(context, socket.socket, "connect", refuse)


@given("no course token and no gateway address in the environment")
def no_token(context):
    assert "COURSE_MCP_TOKEN" not in os.environ and "GATEWAY_URL" not in os.environ


@given('PREFIX is "{prefix}" and AWS_REGION is "{region}"')
def prefix_region(context, prefix, region):
    os.environ["PREFIX"], os.environ["AWS_REGION"] = prefix, region


@given("a product priced {price:g} CAD")
def priced(context, price):
    context.price = price


# ---- when -------------------------------------------------------------------------------------------------------

def _main_run(context, options: str, *extra):
    import main.run as run_module

    context.before = _outputs()
    argv = options.split() + ["--out", str(context.tmp)] + list(extra)
    run(context, run_module.main, argv=argv)


@when("main/run.py runs with no options")
def run_plain(context):
    _main_run(context, "")


@when("main/run.py runs with no options and the output folder in a temporary folder")
def run_plain_tmp(context):
    _main_run(context, "")


@when('main/run.py runs with "{options}"')
def run_options(context, options):
    _main_run(context, options)


@when("main/run.py runs with that query")
def run_query(context):
    _main_run(context, "", "--queries", str(context.queries_file))


@when('main/run.py runs with "{options}" and those queries')
def run_options_queries(context, options):
    _main_run(context, options, "--queries", str(context.queries_file))


@when('clean_up.py runs with "{options}"')
def clean_options(context, options):
    import clean_up

    run(context, clean_up.main, argv=options.split())


@when("clean_up.py runs")
def clean(context):
    clean_options(context, "")


@when('ext-filters/filters_run.py runs with "{options}"')
def filters_options(context, options):
    import filters_run

    run(context, filters_run.main, argv=options.split())


@when("ext-filters/filters_run.py runs with no options")
def filters_plain(context):
    filters_options(context, "")


@when('ext-model-routing/routing_eval.py runs with "{options}"')
def routing_options(context, options):
    import routing_eval

    context.before = _outputs()
    run(context, routing_eval.main, argv=options.split() + ["--out", str(context.tmp)])


@when("ext-model-routing/routing_eval.py runs with no options and the output folder in a temporary folder")
def routing_plain(context):
    routing_options(context, "")


@when('I search keyword and embedding for "{q}"')
def search_both(context, q):
    context.keyword = context.search.keyword(q)
    context.embedding = context.search.embedding(q)


@when('I search keyword for "{q}"')
def search_keyword(context, q):
    context.keyword = context.search.keyword(q)


@when('I ask the embedding search for {k:d} results for "{q}"')
def search_embedding(context, k, q):
    context.embedding = context.search.embedding(q, k)


@when('I fuse the searches for "{q}", asking for {k:d} results')
def fuse(context, q, k):
    context.query = q
    context.fused = context.search.hybrid(q, k)


@when('I search hybrid for "{q}" with the filter category outdoor')
def filtered(context, q):
    from filters import filtered_hybrid

    context.fused = filtered_hybrid(context.search, q, {"category": ["outdoor"]})


# ---- then -------------------------------------------------------------------------------------------------------

@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.error, context.printed[-2000:])


@then('the keyword search returns "{pid}" first')
def keyword_first(context, pid):
    assert context.keyword[0][0] == pid, context.keyword


@then('the text the embedder was given for P1 is "{text}"')
def embedded_text(context, text):
    assert context.embedded[0] == text, context.embedded


@then('the keyword results are "{a}" with score {sa:f} and "{b}" with score {sb:f}')
def keyword_scores(context, a, sa, b, sb):
    got = [(i, round(s, 4)) for i, s in context.keyword]
    assert got == [(a, sa), (b, sb)], got


@then("there is no keyword result")
def no_keyword(context):
    assert context.keyword == [], context.keyword


@then("the embedding search returns {n:d} results")
def n_embedding(context, n):
    assert len(context.embedding) == n, context.embedding


@then("every product in the top 20 of both lists has the fused score 1/(60 + keyword rank) + 1/(60 + embedding rank)")
def fused_both(context):
    kw = {i: r for r, (i, _) in enumerate(context.search.keyword(context.query, 25), 1)}
    em = {i: r for r, (i, _) in enumerate(context.search.embedding(context.query, 25), 1)}
    both = [i for i in kw if kw[i] <= 20 and em[i] <= 20]
    assert both
    fused = dict(context.fused)
    for i in both:
        assert abs(fused[i] - (1 / (60 + kw[i]) + 1 / (60 + em[i]))) < 1e-12, (i, fused[i], kw[i], em[i])


@then("every product below the keyword top 20 gets only its embedding vote, and none when it is below the embedding top 20 too")
def fused_below(context):
    kw = [i for i, _ in context.search.keyword(context.query, 25)]
    em = {i: r for r, (i, _) in enumerate(context.search.embedding(context.query, 25), 1)}
    fused = dict(context.fused)
    below = kw[20:]
    assert any(em[i] <= 20 for i in below) and any(em[i] > 20 for i in below), [em[i] for i in below]
    for i in below:
        if em[i] <= 20:
            assert abs(fused[i] - 1 / (60 + em[i])) < 1e-12, (i, fused[i], em[i])
        else:
            assert i not in fused, (i, em[i])


def _table(context, header_start):
    return table_rows(context, header_start)


@then("the retrieval table is")
def retrieval_table(context):
    rows = _table(context, "| method |")
    want = [list(r.cells) for r in context.table]
    assert rows == want, rows


@then("the answer table is")
def answer_table(context):
    rows = _table(context, "| correct |")
    assert rows == [list(r.cells) for r in context.table], rows


@then("the embedding row has MRR {mrr} and the hybrid row has MRR {mrr2}")
def mrr_rows(context, mrr, mrr2):
    rows = {r[0]: r for r in _table(context, "| method |")}
    assert rows["embedding"][2] == mrr and rows["hybrid"][2] == mrr2, rows


@then("the embedding row has recall@5 {r} and the hybrid row has recall@5 {r2}")
def recall_rows(context, r, r2):
    rows = {x[0]: x for x in _table(context, "| method |")}
    assert rows["embedding"][1] == r and rows["hybrid"][1] == r2, rows


@then("16 chat calls were sent, each listing 5 products with their ids in square brackets")
def chat_calls(context):
    chats = [b for b in bodies(context) if "messages" in b]
    assert len(chats) == 16, len(chats)
    for b in chats:
        ids = re.findall(r"^\[(P\d+)\] ", b["messages"][-1]["content"], flags=re.M)
        assert len(ids) == 5, b


@then("the answer table shows correct {c} and grounded {g}")
def answer_cg(context, c, g):
    [row] = _table(context, "| correct |")
    assert row[:2] == [c, g], row


@then('the file "{name}" there has {r:d} retrieval rows and {a:d} answer rows')
def rows_file(context, name, r, a):
    data = json.loads((context.tmp / name).read_text())
    assert len(data["retrieval"]["rows"]) == r and len(data["answers"]["rows"]) == a


@then("nothing was written to the recipe's own output folder")
def no_output(context):
    assert _outputs() == context.before


@then('every request went to "{url}" with the course token as the bearer key')
def every_request(context, url):
    assert context.requests
    for r in context.requests:
        assert str(r.url) == url and r.headers["authorization"] == f"Bearer {TOKEN}", r


@then('every embedding request asks for "{model}"')
def embedding_model(context, model):
    assert all(b["model"] == model for b in bodies(context) if "input" in b)


@then("the embedding requests held {a:d}, {b:d} and {c:d} texts, {n:d} different texts in all")
def batches(context, a, b, c, n):
    sizes = [len(x["input"]) for x in bodies(context) if "input" in x]
    texts = [t for x in bodies(context) if "input" in x for t in x["input"]]
    assert sizes == [a, b, c] and len(set(texts)) == n == len(texts), sizes


@then("the course token is in no printed line and in no output file")
def token_hidden(context):
    assert TOKEN not in context.printed
    for f in context.tmp.rglob("*"):
        if f.is_file():
            assert TOKEN not in f.read_text(errors="replace"), f


@then('every chat call went to "{url}" with the course token as the bearer key')
def every_chat(context, url):
    chats = [r for r in context.requests if r.url.path.endswith("/chat/completions")]
    assert len(chats) == 16
    for r in chats:
        assert str(r.url) == url and r.headers["authorization"] == f"Bearer {TOKEN}"


@then('every chat call asks for "{model}" with temperature 0 and max_tokens {n:d}')
def chat_shape(context, model, n):
    for b in bodies(context):
        if "messages" in b:
            assert (b["model"], b["temperature"], b["max_tokens"]) == (model, 0, n), b


@then("the recipe waited {s:d} seconds once")
def waited(context, s):
    assert context.sleeps == [s], context.sleeps


@then("the recipe waited between {a:d} and {b:d} seconds once")
def waited_between(context, a, b):
    assert len(context.sleeps) == 1 and a <= context.sleeps[0] <= b, context.sleeps


@then("the first embedding request was sent twice")
def sent_twice(context):
    b = [x for x in bodies(context) if "input" in x]
    assert b[0] == b[1] and len(b) == 3, [len(x["input"]) for x in b]


@then('the run stopped with "{text}"')
def stopped_with(context, text):
    assert text in context.printed, context.printed[-1500:]


@then("the gateway received {n:d} request")
@then("the gateway received {n:d} requests")
def n_requests(context, n):
    assert len(context.requests) == n, len(context.requests)


# ---- OpenSearch -------------------------------------------------------------------------------------------------

@then('the collection group "{name}" is NEXTGEN with standby replicas ENABLED and 0 to 2 OCU for indexing and for search')
def group_made(context, name):
    [p] = [p for p in context.aoss.of("CreateCollectionGroup") if p["name"] == name]
    assert p["generation"] == "NEXTGEN" and p["standbyReplicas"] == "ENABLED"
    assert p["capacityLimits"] == {"minIndexingCapacityInOCU": 0, "minSearchCapacityInOCU": 0,
                                   "maxIndexingCapacityInOCU": 2, "maxSearchCapacityInOCU": 2}, p


@then('the collection "{name}" is a VECTORSEARCH collection in that group with standby replicas ENABLED')
def collection_made(context, name):
    p = context.aoss.collections[name]["params"]
    assert (p["type"], p["collectionGroupName"], p["standbyReplicas"]) == ("VECTORSEARCH", name, "ENABLED"), p


@then('the encryption policy of "{name}" uses a key AWS owns, for that collection only')
def enc_policy(context, name):
    pol = context.aoss.policies[(name, "encryption")]
    assert pol == {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}],
                   "AWSOwnedKey": True}, pol


@then('the network policy of "{name}" allows the public endpoint, for that collection only')
def net_policy(context, name):
    [pol] = context.aoss.policies[(name, "network")]
    assert pol["AllowFromPublic"] is True and pol["Rules"][0]["Resource"] == [f"collection/{name}"], pol


@then('the data access policy of "{name}" names only the identity that ran the script')
def data_policy(context, name):
    import boto3

    me = boto3.client("sts", region_name="ca-central-1").get_caller_identity()["Arn"]
    [pol] = context.aoss.policies[(name, "data")]
    assert pol["Principal"] == [me], pol
    assert [r["Resource"] for r in pol["Rules"]] == [[f"index/{name}/*"], [f"collection/{name}"]], pol


@then("every OpenSearch Serverless call went to {region}")
def aoss_region(context, region):
    assert context.aoss_regions and set(context.aoss_regions) == {region}, set(context.aoss_regions)


def _index(context, name):
    return context.endpoint.indexes[name]


@then('the index "{name}" maps name and description as text, category and brand as keyword, price_cad as float, in_stock as boolean')
def mapping(context, name):
    props = _index(context, name)["body"]["mappings"]["properties"]
    got = {k: v["type"] for k, v in props.items() if k != "embedding"}
    assert got == {"name": "text", "description": "text", "category": "keyword", "brand": "keyword",
                   "price_cad": "float", "in_stock": "boolean"}, got


@then('the index "{name}" has a knn_vector field "{field}" of dimension {dim:d} with cosine similarity')
def knn_field(context, name, field, dim):
    f = _index(context, name)["body"]["mappings"]["properties"][field]
    assert (f["type"], f["dimension"], f["space_type"]) == ("knn_vector", dim, "cosinesimil"), f


@then('the index "{name}" holds {n:d} documents under the product ids')
def docs(context, name, n):
    ids = sorted(_index(context, name)["docs"])
    assert ids == sorted(p["id"] for p in load_products()) and len(ids) == n


def _searches(context):
    return [json.loads(r["body"]) for r in context.endpoint.requests if r["path"].endswith("/_search")]


@then('every search request to the endpoint is signed for the service "{service}"')
def signed(context, service):
    reqs = [r for r in context.endpoint.requests if r["path"].endswith("/_search")]
    assert reqs
    for r in reqs:
        auth = r["headers"].get("Authorization", "")
        assert f"/ca-central-1/{service}/aws4_request" in auth and "x-amz-content-sha256" in auth, auth


@then('each hybrid search sent one multi_match query with "name^2" and one knn query, each for 20 results')
def pairs(context):
    s = _searches(context)
    hybrid = [(a, b) for a, b in zip(s, s[1:]) if "multi_match" in a["query"] and "knn" in b["query"]
              and a["size"] == 20]
    assert len(hybrid) == 32, len(hybrid)     # 16 queries: the hybrid eval and the answer eval
    for a, b in hybrid:
        assert "name^2" in a["query"]["multi_match"]["fields"] and b["size"] == 20
        assert b["query"]["knn"]["embedding"]["k"] == 20


@then("the hybrid results are the reciprocal rank fusion of the two answers, k = 60")
def fused_os(context):
    data = json.loads(next(context.tmp.glob("eval-opensearch-*.json")).read_text())
    rows = [r for r in data["retrieval"]["rows"] if r["method"] == "hybrid"]
    s = _searches(context)
    hybrid = [(a, b) for a, b in zip(s, s[1:]) if "multi_match" in a["query"] and "knn" in b["query"]
              and a["size"] == 20][:16]
    ep = context.endpoint
    for row, (a, b) in zip(rows, hybrid):
        votes = {}
        for q in (a, b):
            for rank, h in enumerate(ep._search("products", q), 1):
                votes[h["_id"]] = votes.get(h["_id"], 0) + 1 / (60 + rank)
        want = [i for i, _ in sorted(votes.items(), key=lambda x: (-x[1], x[0]))][:5]
        assert row["top"] == want, (row["top"], want)


@then("there is {g:d} collection group, {c:d} collection and {p:d} policies")
def counts(context, g, c, p):
    a = context.aoss
    assert (len(a.groups), len(a.collections), len(a.policies)) == (g, c, p)


@then('the collection "{name}" was created after the old one was gone')
def created_after(context, name):
    c = context.aoss.collections[name]
    assert not c.get("old") and c["status"] == "ACTIVE"
    ops = [op for op, _ in context.aoss.calls]
    first_create = ops.index("CreateCollection")
    reads = [i for i, (op, p) in enumerate(context.aoss.calls) if op == "BatchGetCollection"]
    assert reads and reads[0] < first_create


@then("CreateCollectionGroup was called twice and the collection is in the group")
def group_twice(context):
    assert len(context.aoss.of("CreateCollectionGroup")) == 2
    assert context.aoss.collections["recipe-ana-products"]["group"] in context.aoss.groups


# ---- clean-up ---------------------------------------------------------------------------------------------------

def _lines(context, verb):
    return [x for x in context.printed.splitlines() if x.startswith(verb)]


@then('it printed "removed" for the collection, the group, the data access, network and encryption policies of "{name}"')
def removed_all(context, name):
    got = sorted(x.split(None, 1)[1] for x in _lines(context, "removed "))
    assert got == sorted([f"collection {name}", f"collection group {name}", f"data access policy {name}",
                          f"network policy {name}", f"encryption policy {name}"]), got


@then('nothing named "{name}" is left')
def nothing_left(context, name):
    a = context.aoss
    assert name not in a.collections and name not in a.groups and not [k for k in a.policies if k[0] == name]


@then("the teammate's collection, group and policies are still there")
def teammate_kept(context):
    a = context.aoss
    assert "recipe-bo-products" in a.collections and "recipe-bo-products" in a.groups
    assert len([k for k in a.policies if k[0] == "recipe-bo-products"]) == 3


@then('it printed "{verb}" {n:d} times')
def printed_n(context, verb, n):
    assert len(_lines(context, verb + " ")) == n, context.printed


@then("no delete call was made")
def no_delete(context):
    assert not [op for op, _ in context.aoss.calls if op.startswith("Delete")]


@then('it printed "FAILED" for the collection with "{code}"')
def failed(context, code):
    lines = [x for x in _lines(context, "FAILED") if x.split(None, 1)[1].startswith("collection recipe-ana-products:")]
    assert len(lines) == 1 and code in lines[0], context.printed


@then('it printed "removed" for the data access, network and encryption policies of "{name}"')
def removed_policies(context, name):
    got = {x.split(None, 1)[1] for x in _lines(context, "removed ")}
    assert {f"data access policy {name}", f"network policy {name}", f"encryption policy {name}"} <= got, got


@then('it printed "absent" for "{what}"')
def absent_line(context, what):
    assert any(x.split(None, 1)[1] == what for x in _lines(context, "absent ")), context.printed


@then("the group was deleted after the collection was gone")
def group_after(context):
    res = context.aoss.results
    i = next(i for i, (op, _) in enumerate(res) if op == "DeleteCollectionGroup")
    last_read = [out for op, out in res[:i] if op == "BatchGetCollection"][-1]
    assert last_read["collectionDetails"] == [], last_read


@then("the last call about the group found no group")
def group_gone(context):
    res = context.aoss.results
    i = next(i for i, (op, _) in enumerate(res) if op == "DeleteCollectionGroup")
    reads = [out for op, out in res[i:] if op == "BatchGetCollectionGroup"]
    assert reads and reads[-1]["collectionGroupDetails"] == [], reads


@then('the collection, the group and the three policies are named "{name}"')
def named(context, name):
    a = context.aoss
    assert list(a.groups) == [name] and list(a.collections) == [name]
    assert sorted(a.policies) == [(name, "data"), (name, "encryption"), (name, "network")]


# ---- extensions -------------------------------------------------------------------------------------------------

@then("the filter table is")
def filter_table(context):
    rows = [[r[0]] + r[2:] for r in _table(context, "| query | filter |")]
    assert rows == [list(r.cells) for r in context.table], rows


@then('it printed "{text}"')
def printed(context, text):
    assert text in context.printed, context.printed[-1500:]


@then('every result is in the category "{cat}"')
def all_cat(context, cat):
    products = context.search.products
    assert context.fused and all(products[i]["category"] == cat for i, _ in context.fused)


@then('the result holds the two-person tent "{pid}"')
def holds(context, pid):
    assert pid in [i for i, _ in context.fused]


@then("it printed the facets {facets}")
def printed_facets(context, facets):
    assert f"facets for the keyword matches of 'wireless': {facets}" in context.printed, context.printed


@then('its facet price band is "{band}"')
def band(context, band):
    from filters import facets

    f = facets({"X": {"category": "c", "brand": "b", "price_cad": context.price}}, {"X"})
    assert [k for k, v in f["price"].items() if v] == [band], f


@then("every multi_match query of a filtered search is in a bool query with the filter clause")
def mm_filter(context):
    s = [q for q in _searches(context) if "bool" in q["query"]]
    assert len(s) == 7, len(s)
    for q in s:
        assert q["query"]["bool"]["filter"] and "multi_match" in q["query"]["bool"]["must"][0]


@then("every knn query of a filtered search carries the filter clause inside the knn query")
def knn_filter(context):
    s = [q for q in _searches(context) if "knn" in q["query"] and "filter" in q["query"]["knn"]["embedding"]]
    assert len(s) == 7, len(s)
    for q in s:
        assert q["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]


def _routing(context):
    return json.loads((context.tmp / "routing-gateway.json").read_text())


@then('every router call asks for "{model}" with max_tokens {n:d}')
def router_calls(context, model, n):
    from router import ROUTER_SYSTEM

    calls = [b for b in bodies(context) if b["messages"][0]["content"] == ROUTER_SYSTEM]
    assert len(calls) == 10 and all((b["model"], b["max_tokens"]) == (model, n) for b in calls)


@then('in the routed policy the request "{text}" is answered by "{model}"')
def routed_one(context, text, model):
    from main.config import load_jsonl
    from routing_eval import HERE

    rid = next(r["id"] for r in load_jsonl(HERE / "requests.jsonl") if r["text"] == text)
    routed = next(p for p in _routing(context) if p["policy"] == "routed")
    assert next(r["model"] for r in routed["rows"] if r["id"] == rid) == model


@then('in the routed policy every other request is answered by "{model}"')
def routed_others(context, model):
    routed = next(p for p in _routing(context) if p["policy"] == "routed")
    assert sorted(r["model"] for r in routed["rows"]).count(model) == 9


@then('in the routed policy every request is answered by "{model}"')
def routed_all(context, model):
    routed = next(p for p in _routing(context) if p["policy"] == "routed")
    assert {r["model"] for r in routed["rows"]} == {model}


@then("the policy table is")
def policy_table(context):
    rows = _table(context, "| policy |")
    assert rows == [list(r.cells) for r in context.table], rows


@then("the {policy} policy cost is {n:d} calls of {i:d} input and {o:d} output tokens at {pi:g} and {po:g} CAD per million")
def policy_cost(context, policy, n, i, o, pi, po):
    row = next(r for r in _table(context, "| policy |") if r[0] == policy)
    assert row[2] == f"{n * (i * pi + o * po) / 1e6:.6f}", row


@then('it printed "FAILED" for the group, which cannot go while its collection is there')
def failed_group(context):
    lines = [x for x in _lines(context, "FAILED") if "collection group recipe-ana-products" in x]
    assert len(lines) == 1 and "ConflictException" in lines[0], context.printed


@given("the collection's endpoint answers 401 to the first {n:d} requests")
def endpoint_denies(context, n):
    context.endpoint.deny_first = n


@then("the recipe waited {s:d} seconds {n:d} times")
def waited_n(context, s, n):
    assert context.sleeps.count(s) == n, context.sleeps


@given("a collection create that names a missing group is refused with {code}")
def missing_group_code(context, code):
    context.aoss.group_missing_code = code


@given("the gateway is a stand-in that refuses the first call with HTTP {status:d} and a message that holds the key")
def gateway_echo(context, status):
    support.use_gateway(context, refusals=[(status, "invalid_key", None)], echo_key=True)
@given("the gateway is a stand-in that answers the {path} calls with status 200 and {answer}")
def gateway_malformed(context, path, answer):
    support.use_gateway(context, malformed=answer, on="/embeddings" if path == "embeddings" else "/completions")


@given("the gateway is a stand-in that does not answer within the time limit")
def gateway_silent(context):
    support.use_gateway(context, times_out=True)


@then("no line of the output is a Python traceback")
def no_traceback(context):
    assert "Traceback" not in context.printed and context.error is None, context.printed[-800:]


@then("that request carried a read time limit of {s:d} seconds")
def read_limit(context, s):
    [req] = context.requests
    assert req.extensions["timeout"]["read"] == s, req.extensions["timeout"]


@then('no printed line starts with "{start}"')
def no_line(context, start):
    assert not [x for x in context.printed.splitlines() if x.startswith(start)], context.printed


@when('main/run.py runs with that query and "{options}"')
def run_query_options(context, options):
    _main_run(context, options, "--queries", str(context.queries_file))


@then("the retrieval table was printed and the rows were written")
def table_and_rows(context):
    assert _table(context, "| method |") and (context.tmp / "eval-local-stub-stub.json").is_file()


@given("the labelled queries {lines}")
def labelled_lines(context, lines):
    rows = []
    for part in lines.split(", then "):
        m = re.match(r'(\w+) "(.*)" relevant (.*)$', part)
        qid, text, rel = m.groups()
        rows.append({"id": qid, "query": text, "kind": "plain words",
                     "relevant": [] if rel == "none" else rel.split()})
    context.queries_file = context.tmp / "queries.jsonl"
    context.queries_file.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run_record(context):
    return json.loads((context.tmp / "eval-local-stub-stub.json").read_text())["run"]


@then('the rows file\'s run record names the SHA-256 of "{a}" and of "{b}" as they are on disk')
def run_hashes(context, a, b):
    import hashlib

    rec = _run_record(context)["data"]
    assert rec[a] == hashlib.sha256((ROOT / "data" / a).read_bytes()).hexdigest()
    assert rec[b] == hashlib.sha256(context.queries_file.read_bytes()).hexdigest()


@then("the rows file's run record has embedder \"{e}\", answers \"{a}\", k {k:d}, rrf_k {rk:d}, pool {pool:d}, bm25_k1 {k1:g} and bm25_b {b:g}")
def run_params(context, e, a, k, rk, pool, k1, b):
    rec = _run_record(context)
    got = (rec["embedder"], rec["answers"], rec["k"], rec["rrf_k"], rec["pool"], rec["bm25_k1"], rec["bm25_b"])
    assert got == (e, a, k, rk, pool, k1, b), got


@given("the gateway is a stand-in whose connection fails")
def gateway_no_connection(context):
    support.use_gateway(context, refuses_connection=True)


@given('a labelled query "{query}" whose relevant products are {a}, {b} and {c}')
def three_relevant(context, query, a, b, c):
    context.queries_file = context.tmp / "queries.jsonl"
    context.queries_file.write_text(json.dumps({"id": "q1", "query": query, "kind": "plain words",
                                                "relevant": [a, b, c]}) + "\n")


@then("the hybrid row has recall@5 {r}")
def hybrid_recall(context, r):
    rows = {x[0]: x for x in _table(context, "| method |")}
    assert rows["hybrid"][1] == r, rows


@then("no table was printed")
def no_table(context):
    assert "| method |" not in context.printed, context.printed
