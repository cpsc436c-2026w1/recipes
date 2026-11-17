"""Stand-ins and helpers the step files share. None of them opens a network connection.

Gateway      the recipe's own clients (main/llm.py GatewayChat, main/embed.py GatewayEmbedder) keep the address,
             headers and time limit they built; only their HTTP transport is replaced (httpx.MockTransport).
Aoss         OpenSearch Serverless's control plane, answered where boto3 hands a call to the network
             (botocore's BaseClient._make_api_call), with the error codes of the API reference. Other services
             (STS, IAM) go on to moto.
Endpoint     the collection's endpoint, answered where `requests` sends a prepared request (HTTPAdapter.send), so
             opensearch-py and its SigV4 signer run as they do against AWS.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import threading

import botocore.client
import httpx

GATEWAY_URL = "https://gw.example/v1"     # made up: the stand-in's transport never leaves the process
TOKEN = "tok-hybrid-made-up-123"          # a made-up course token
REQUEST_LIMIT = 80                        # a stand-in answers at most this many requests: a loop fails, not hangs
CALL_LIMIT = 300                          # the same for the OpenSearch Serverless stand-in


def patch(context, obj, name: str, value) -> None:
    """Replace an attribute for this scenario only."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)


def run(context, fn, *args, argv: list[str] | None = None, **kwargs):
    """Call fn; keep what it printed, its exit code (0 when it returned; SystemExit with a message is 1, as in a
    shell) and, when it raised, the exception."""
    out = io.StringIO()
    context.exit_code, context.error = 0, None
    result = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = fn(argv) if argv is not None else fn(*args, **kwargs)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                out.write(str(e.code) + "\n")
        except Exception as e:  # noqa: BLE001 - a run that stops with an error; the scenario reads which
            context.exit_code, context.error = 1, e
            out.write(f"{type(e).__name__}: {e}\n")
    context.printed += out.getvalue()
    return result


# ---- the course LLM gateway -------------------------------------------------------------------------------------

def vector(text: str, dim: int = 8) -> list[float]:
    """A made-up embedding: the same text always gets the same vector."""
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255 for b in digest[:dim]]


class Gateway:
    """Answers /embeddings and /chat/completions. `refusals` is a list of (status, code, retry_after) for the first
    calls (one per call); `reply(body)` gives a chat reply (default: cite the first product shown)."""

    def __init__(self, requests: list, refusals=(), reply=None, short_by: int = 0, echo_key: bool = False,
                 malformed: str = "", on: str = "", times_out: bool = False, refuses_connection: bool = False):
        self.requests, self.refusals, self.reply, self.short_by = requests, list(refusals), reply, short_by
        self.echo_key = echo_key       # a refusal whose message repeats the bearer key it was sent
        self.malformed, self.on, self.times_out = malformed, on, times_out   # on: "embeddings" or "chat"
        self.refuses_connection = refuses_connection

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert len(self.requests) <= REQUEST_LIMIT, "the gateway stand-in received more requests than a run needs"
        if self.refusals:
            status, code, retry_after = self.refusals.pop(0)
            headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
            message = f"refused key {request.headers['authorization']}" if self.echo_key else "refused"
            return httpx.Response(status, headers=headers, json={"error": {"code": code, "message": message}})
        if self.times_out:
            raise httpx.ReadTimeout("the stand-in did not answer", request=request)
        if self.refuses_connection:
            raise httpx.ConnectError("the stand-in refused the connection", request=request)
        if self.malformed and request.url.path.endswith(self.on):
            if self.malformed == "a page that is not JSON":
                return httpx.Response(200, text="<html>502 Bad Gateway</html>")
            if self.malformed == "an error object":
                return httpx.Response(200, json={"error": {"message": "upstream failed", "code": "upstream_error"}})
            if self.malformed == "no list":
                return httpx.Response(200, json={"usage": {"prompt_tokens": 5}})
            raise AssertionError(self.malformed)
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            texts = body["input"][: len(body["input"]) - self.short_by]
            return httpx.Response(200, json={"data": [{"index": i, "embedding": vector(t)} for i, t in
                                                      enumerate(texts)], "usage": {"prompt_tokens": 5 * len(texts)}})
        user = body["messages"][-1]["content"]
        if self.reply:
            text = self.reply(body)
        else:
            ids = re.findall(r"^\[(P\d+)\]", user, flags=re.M)
            text = f"Try [{ids[0]}]." if ids else "None fits."
        finish = "stop"
        if isinstance(text, tuple):   # reply(body) may give (text, finish_reason)
            text, finish = text
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 400, "completion_tokens": 20}})


def use_gateway(context, **kw) -> Gateway:
    """Every gateway client the recipe builds in this scenario gets the stand-in as its transport."""
    from main import embed, llm

    gw = Gateway(context.requests, **kw)
    context.gateway = gw
    for cls in (llm.GatewayChat, embed.GatewayEmbedder):
        built = cls.__init__

        def init(self, *a, _built=built, **k):
            _built(self, *a, **k)
            self.sleep = lambda s: context.sleeps.append(s)   # bound at definition time: replace it here
            http = self.http
            self.http = httpx.Client(base_url=http.base_url, headers=http.headers, timeout=http.timeout,
                                     transport=httpx.MockTransport(gw))

        patch(context, cls, "__init__", init)
    # the waits of a refusal are written down, not waited
    import main.llm as llm_module

    patch(context, llm_module.time, "sleep", lambda s: context.sleeps.append(s))
    return gw


def bodies(context) -> list[dict]:
    return [json.loads(r.content) for r in context.requests]


# ---- OpenSearch Serverless, control plane --------------------------------------------------------------------------

class Aoss:
    """What CreateCollectionGroup, CreateCollection and the policy calls answer, after the API reference
    (https://docs.aws.amazon.com/opensearch-service/latest/ServerlessAPIReference/). A collection is CREATING for
    `polls_to_active` reads, then ACTIVE; a deleted one is DELETING for `polls_to_gone` reads, then gone. A
    NextGen group refuses standbyReplicas DISABLED, as it did in the sandbox on 2026-11-12."""

    def __init__(self, polls_to_active: int = 2, polls_to_gone: int = 2):
        self.policies: dict[tuple[str, str], dict] = {}
        self.groups: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.results: list[tuple[str, dict]] = []   # the answers that were not errors, in order
        self.polls_to_active, self.polls_to_gone = polls_to_active, polls_to_gone
        self.refuse: dict[str, str] = {}        # operation -> error code, answered every time
        self.collection_never_active = False
        self.group_missing_code = "ResourceNotFoundException"   # the API reference lists none for this case

    def answer(self, client, operation: str, p: dict):
        self.calls.append((operation, dict(p)))
        assert len(self.calls) <= CALL_LIMIT, f"{CALL_LIMIT} calls to OpenSearch Serverless: a loop"
        if operation in self.refuse:
            raise self._error(client, self.refuse[operation], operation)
        if operation in ("CreateSecurityPolicy", "CreateAccessPolicy"):
            kind = p["type"]
            if (p["name"], kind) in self.policies:
                raise self._error(client, "ConflictException", operation)
            self.policies[(p["name"], kind)] = json.loads(p["policy"])
            return {}
        if operation in ("GetSecurityPolicy", "GetAccessPolicy"):
            if (p["name"], p["type"]) not in self.policies:
                raise self._error(client, "ResourceNotFoundException", operation)
            return {"accessPolicyDetail" if "Access" in operation else "securityPolicyDetail":
                    {"name": p["name"], "type": p["type"], "policy": self.policies[(p["name"], p["type"])],
                     "policyVersion": "v1"}}
        if operation in ("DeleteSecurityPolicy", "DeleteAccessPolicy"):
            if self.policies.pop((p["name"], p["type"]), None) is None:
                raise self._error(client, "ResourceNotFoundException", operation)
            return {}
        if operation == "CreateCollectionGroup":
            if p["generation"] == "NEXTGEN" and p["standbyReplicas"] != "ENABLED":
                raise self._error(client, "ValidationException", operation,
                                  "StandbyReplicas cannot be set to DISABLED for NEXTGEN collection groups.")
            if p["name"] in self.groups:
                raise self._error(client, "ConflictException", operation)
            self.groups[p["name"]] = {"id": f"g-{p['name']}", **p}
            return {"createCollectionGroupDetail": {"id": f"g-{p['name']}", "name": p["name"]}}
        if operation == "BatchGetCollectionGroup":
            found = []
            for name in p.get("names", []):
                g = self.groups.get(name)
                if g and g.get("deleting") is not None:
                    g["deleting"] -= 1
                    if g["deleting"] < 0:
                        del self.groups[name]
                        continue
                if g:
                    found.append({"id": g["id"], "name": name})
            return {"collectionGroupDetails": found, "collectionGroupErrorDetails": []}
        if operation == "DeleteCollectionGroup":
            name = next((n for n, g in self.groups.items() if g["id"] == p["id"]), None)
            if name is None:
                raise self._error(client, "ResourceNotFoundException", operation)
            if any(c["group"] == name for c in self.collections.values()):
                raise self._error(client, "ConflictException", operation, "the group still has collections")
            self.groups[name]["deleting"] = self.polls_to_gone
            return {}
        if operation == "CreateCollection":
            g = self.groups.get(p.get("collectionGroupName"))
            if g and g.get("vanish"):                  # it was being deleted: "already exists", then gone
                del self.groups[p["collectionGroupName"]]
            if p["name"] in self.collections:
                raise self._error(client, "ConflictException", operation)
            if p.get("collectionGroupName") not in self.groups:
                raise self._error(client, self.group_missing_code, operation,
                                  f"collection group {p.get('collectionGroupName')} not found")
            self.collections[p["name"]] = {"id": f"c-{p['name']}", "name": p["name"], "type": p["type"],
                                           "group": p["collectionGroupName"], "status": "CREATING",
                                           "polls": self.polls_to_active, "params": dict(p)}
            return {"createCollectionDetail": {"id": f"c-{p['name']}", "name": p["name"], "status": "CREATING"}}
        if operation == "BatchGetCollection":
            out = []
            for name in p.get("names", []):
                c = self.collections.get(name)
                if c is None:
                    continue
                c["polls"] -= 1
                if c["status"] == "CREATING" and c["polls"] < 0 and not self.collection_never_active:
                    c["status"] = "ACTIVE"
                if c["status"] == "DELETING" and c["polls"] < 0:
                    del self.collections[name]
                    continue
                out.append({"id": c["id"], "name": name, "status": c["status"], "arn": f"arn:aws:aoss:{name}",
                            "collectionEndpoint": f"https://{c['id']}.ca-central-1.aoss.amazonaws.com"})
            return {"collectionDetails": out, "collectionErrorDetails": []}
        if operation == "DeleteCollection":
            c = next((c for c in self.collections.values() if c["id"] == p["id"]), None)
            if c is None:
                raise self._error(client, "ResourceNotFoundException", operation)
            c["status"], c["polls"] = "DELETING", self.polls_to_gone
            return {"deleteCollectionDetail": {"id": c["id"], "status": "DELETING"}}
        raise AssertionError(f"the OpenSearch Serverless stand-in has no answer for {operation}")

    @staticmethod
    def _error(client, code, operation, message="put in by the stand-in"):
        return client.exceptions.from_code(code)({"Error": {"Code": code, "Message": message}}, operation)

    def of(self, operation: str) -> list[dict]:
        return [p for op, p in self.calls if op == operation]


def use_aoss(context, aoss: Aoss) -> Aoss:
    real = botocore.client.BaseClient._make_api_call
    lock = threading.Lock()

    def make_api_call(client, operation, params):
        if client.meta.service_model.service_name == "opensearchserverless":
            with lock:
                out = aoss.answer(client, operation, params)
                aoss.results.append((operation, out))
                return out
        return real(client, operation, params)

    patch(context, botocore.client.BaseClient, "_make_api_call", make_api_call)
    context.aoss = aoss
    return aoss


# ---- the collection's endpoint ------------------------------------------------------------------------------------

class Endpoint:
    """An index behind the collection's endpoint: create, exists, _bulk, _count, and _search with a multi_match
    (documents ranked by how many query words their text fields hold) or a knn query (dot product). Ranking is
    simpler than OpenSearch's: the scenarios check what the recipe sends and how it fuses what comes back."""

    def __init__(self, deny_first: int = 0):
        self.indexes: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.deny_first = deny_first

    def send(self, request):
        import requests

        from urllib.parse import urlparse

        url = urlparse(request.url)
        body = request.body.decode() if isinstance(request.body, bytes) else (request.body or "")
        self.requests.append({"method": request.method, "path": url.path, "body": body,
                              "headers": dict(request.headers)})
        assert len(self.requests) <= REQUEST_LIMIT * 4, "the endpoint stand-in received a loop of requests"
        if self.deny_first > 0:
            self.deny_first -= 1
            status, out = 401, {"message": "refused by the stand-in"}
        else:
            status, out = self._answer(request.method, url.path, body)
        resp = requests.Response()
        resp.status_code, resp._content = status, json.dumps(out).encode()
        resp.headers["content-type"] = "application/json"
        resp.request, resp.url = request, request.url
        return resp

    def _answer(self, method, path, body):
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "_bulk":
            lines = [json.loads(x) for x in body.splitlines() if x.strip()]
            items = []
            for meta, src in zip(lines[::2], lines[1::2]):
                (action, m), = meta.items()
                self.indexes.setdefault(m["_index"], {"docs": {}})["docs"][m["_id"]] = src
                items.append({action: {"_id": m["_id"], "status": 201}})
            return 200, {"errors": False, "items": items}
        index = parts[0]
        if len(parts) == 1:
            if method == "HEAD":
                return (200 if index in self.indexes else 404), {}
            if method == "PUT":
                self.indexes[index] = {"docs": {}, "body": json.loads(body)}
                return 200, {"acknowledged": True}
        if parts[1:] == ["_count"]:
            return 200, {"count": len(self.indexes[index]["docs"])}
        if parts[1:] == ["_search"]:
            q = json.loads(body)
            return 200, {"hits": {"hits": self._search(index, q)}}
        raise AssertionError(f"the endpoint stand-in has no answer for {method} {path}")

    def _search(self, index, q):
        docs = self.indexes[index]["docs"]
        query = q["query"]
        if "knn" in query:
            v = query["knn"]["embedding"]["vector"]
            scored = [(sum(a * b for a, b in zip(v, d["embedding"])), i) for i, d in docs.items()]
        else:
            mm = query.get("multi_match") or query["bool"]["must"][0]["multi_match"]
            words = set(re.findall(r"\w+", mm["query"].lower()))
            scored = []
            for i, d in docs.items():
                text = " ".join(str(d.get(f.split("^")[0], "")) for f in mm["fields"]).lower()
                n = len(words & set(re.findall(r"\w+", text)))
                if n:
                    scored.append((float(n), i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{"_id": i, "_score": s} for s, i in scored[: q.get("size", 10)]]


def use_endpoint(context, endpoint: Endpoint) -> Endpoint:
    import requests.adapters

    patch(context, requests.adapters.HTTPAdapter, "send", lambda self, request, **kw: endpoint.send(request))
    context.endpoint = endpoint
    return endpoint


def no_waits(context, *modules) -> None:
    """The recipe's waits are written down in context.sleeps, and a clock moves on by each wait, so a deadline
    passes in a scenario without anyone waiting."""
    clock = {"now": 1_000_000.0}

    def sleep(s):
        context.sleeps.append(s)
        clock["now"] += s

    def now():
        clock["now"] += 0.001
        return clock["now"]

    import types

    fake = types.SimpleNamespace(sleep=sleep, time=now)   # the recipe module's own `time`; the global one is untouched
    for m in modules:
        patch(context, m, "time", fake)


def stdout_lines(context) -> list[str]:
    return [line for line in context.printed.splitlines() if line.strip()]


def table_rows(context, header_start: str) -> list[list[str]]:
    """The rows of the first printed markdown table whose header starts with `header_start`."""
    lines = context.printed.splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith(header_start))
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def unload(*names: str) -> None:
    for n in names:
        sys.modules.pop(n, None)
