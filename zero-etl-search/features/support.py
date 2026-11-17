"""Stand-ins and helpers the step files share. None of them opens a network connection.

Calls      sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call). It writes down every
           call and answers, in place of AWS: OpenSearch Serverless's control plane (Aoss below), Lambda's Invoke
           (the function's probe; moto would run the function in a container), and any refusal a scenario names,
           with the error code of the API reference. Every other call goes on to moto.
Endpoint   the collection's endpoint for the scripts (opensearch-py and requests), answered at HTTPAdapter.send, so
           the SigV4 signer runs as against AWS.
UrlIndex   the collection's endpoint for the function (main/handler.py sends with urllib): urlopen answered by the
           recipe's own stand-in index (main/standin.py StandInIndex), or by a scripted failure.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading

import botocore.client

CALL_LIMIT = 400        # a scenario must fail, not hang: this many calls end it
REQUEST_LIMIT = 200


def patch(context, obj, name: str, value) -> None:
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)


def run(context, fn, argv=None):
    """Call fn(argv); keep what it printed (context.printed: both streams; context.stdout: standard output only),
    its exit code and an error."""
    out, err_out = io.StringIO(), io.StringIO()
    context.exit_code, context.error = 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_out):
        try:
            fn(argv)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                out.write(str(e.code) + "\n")
        except Exception as e:  # noqa: BLE001 - the scenario reads which error
            context.exit_code, context.error = 1, e
            out.write(f"{type(e).__name__}: {e}\n")
    context.printed += out.getvalue() + err_out.getvalue()
    context.stdout = out.getvalue()


def no_waits(context, *modules) -> None:
    """Waits are written down in context.sleeps; the clock moves on by each wait, so deadlines pass at once."""
    clock = {"now": 1_700_000_000.0}

    def sleep(s):
        context.sleeps.append(s)
        clock["now"] += s

    def now():
        clock["now"] += 0.001
        return clock["now"]

    import time

    patch(context, time, "sleep", sleep)
    patch(context, time, "time", now)


class Aoss:
    """OpenSearch Serverless's control plane, after its API reference. A collection is CREATING for two reads, then
    ACTIVE; a deleted one DELETING for two reads, then gone. A group holds one type of collection and cannot be
    deleted while it holds one. UpdateAccessPolicy needs the current policyVersion."""

    def __init__(self):
        self.policies: dict[tuple[str, str], dict] = {}
        self.versions: dict[tuple[str, str], int] = {}
        self.groups: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}

    def _error(self, client, code, op, message="put in by the stand-in"):
        return client.exceptions.from_code(code)({"Error": {"Code": code, "Message": message}}, op)

    def answer(self, client, op, p):
        if op in ("CreateSecurityPolicy", "CreateAccessPolicy"):
            key = (p["name"], p["type"])
            if key in self.policies:
                raise self._error(client, "ConflictException", op)
            pol = json.loads(p["policy"])
            if p["type"] == "data" and not pol:
                raise self._error(client, "ValidationException", op, "Policy json is invalid")
            self.policies[key], self.versions[key] = pol, 1
            return {}
        if op in ("GetSecurityPolicy", "GetAccessPolicy"):
            key = (p["name"], p["type"])
            if key not in self.policies:
                raise self._error(client, "ResourceNotFoundException", op)
            name = "accessPolicyDetail" if op == "GetAccessPolicy" else "securityPolicyDetail"
            return {name: {"name": key[0], "type": key[1], "policy": self.policies[key],
                           "policyVersion": f"v{self.versions[key]}"}}
        if op == "UpdateAccessPolicy":
            key = (p["name"], p["type"])
            if p["policyVersion"] != f"v{self.versions[key]}":
                raise self._error(client, "ConflictException", op, "policy version changed")
            self.policies[key] = json.loads(p["policy"])
            self.versions[key] += 1
            return {}
        if op in ("DeleteSecurityPolicy", "DeleteAccessPolicy"):
            if self.policies.pop((p["name"], p["type"]), None) is None:
                raise self._error(client, "ResourceNotFoundException", op)
            return {}
        if op == "CreateCollectionGroup":
            if p["generation"] == "NEXTGEN" and p["standbyReplicas"] != "ENABLED":
                raise self._error(client, "ValidationException", op,
                                  "StandbyReplicas cannot be set to DISABLED for NEXTGEN collection groups.")
            if p["name"] in self.groups:
                raise self._error(client, "ConflictException", op)
            self.groups[p["name"]] = {"id": f"g-{p['name']}", **p}
            return {}
        if op == "BatchGetCollectionGroup":
            out = []
            for n in p.get("names", []):
                g = self.groups.get(n)
                if g and g.get("deleting") is not None:
                    g["deleting"] -= 1
                    if g["deleting"] < 0:
                        del self.groups[n]
                        continue
                if g:
                    out.append({"id": g["id"], "name": n})
            return {"collectionGroupDetails": out, "collectionGroupErrorDetails": []}
        if op == "DeleteCollectionGroup":
            n = next((n for n, g in self.groups.items() if g["id"] == p["id"]), None)
            if n is None:
                raise self._error(client, "ResourceNotFoundException", op)
            if any(c["group"] == n for c in self.collections.values()):
                raise self._error(client, "ConflictException", op, "the group still has collections")
            self.groups[n]["deleting"] = 2
            return {}
        if op == "CreateCollection":
            g = self.groups.get(p.get("collectionGroupName"))
            if g is None:
                raise self._error(client, "ResourceNotFoundException", op, "collection group not found")
            kinds = {c["type"] for c in self.collections.values() if c["group"] == p["collectionGroupName"]}
            if kinds and p["type"] not in kinds:   # docs: one type of collection per group
                raise self._error(client, "ValidationException", op, "a collection group holds one collection type")
            if p["name"] in self.collections:
                raise self._error(client, "ConflictException", op)
            self.collections[p["name"]] = {"id": f"c-{p['name']}", "type": p["type"], "status": "CREATING",
                                           "group": p["collectionGroupName"], "polls": 2, "params": dict(p)}
            return {"createCollectionDetail": {"id": f"c-{p['name']}", "name": p["name"]}}
        if op == "BatchGetCollection":
            out = []
            for n in p.get("names", []):
                c = self.collections.get(n)
                if c is None:
                    continue
                c["polls"] -= 1
                if c["status"] == "CREATING" and c["polls"] < 0:
                    c["status"] = "ACTIVE"
                if c["status"] == "DELETING" and c["polls"] < 0:
                    del self.collections[n]
                    continue
                out.append({"id": c["id"], "name": n, "status": c["status"],
                            "arn": f"arn:aws:aoss:ca-central-1:123456789012:collection/{c['id']}",
                            "collectionEndpoint": f"https://{c['id']}.ca-central-1.aoss.amazonaws.com"})
            return {"collectionDetails": out, "collectionErrorDetails": []}
        if op == "DeleteCollection":
            c = next((c for c in self.collections.values() if c["id"] == p["id"]), None)
            if c is None:
                raise self._error(client, "ResourceNotFoundException", op)
            c["status"], c["polls"] = "DELETING", 2
            return {}
        raise AssertionError(f"the OpenSearch Serverless stand-in has no answer for {op}")


class Calls:
    def __init__(self, aoss: Aoss):
        self.aoss = aoss
        self.made: list[tuple[str, str, dict]] = []      # (service, operation, parameters)
        self.regions: list[str] = []
        self.refusals: list[dict] = []
        self.invoke_answers: list[dict] = []             # what the probe answers, in order; then ok
        self._lock = threading.Lock()

    def refuse(self, operation, code, times=-1, message="put in by the scenario", response_code=None):
        """`code` picks the exception class; `response_code` is the code in the response when the service sends
        another one (SQS sends AWS.SimpleQueueService.QueueDeletedRecently for the class QueueDeletedRecently)."""
        self.refusals.append({"op": operation, "code": code, "left": times, "message": message,
                              "response_code": response_code or code})

    def start(self, context):
        real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            service = client.meta.service_model.service_name
            with calls._lock:
                calls.made.append((service, operation, dict(params)))
                calls.regions.append(client.meta.region_name)
                assert len(calls.made) <= CALL_LIMIT, f"{CALL_LIMIT} AWS calls: a loop"
                rule = next((r for r in calls.refusals if r["op"] == operation and r["left"] != 0), None)
                if rule:
                    rule["left"] -= 1
                    raise client.exceptions.from_code(rule["code"])(
                        {"Error": {"Code": rule["response_code"], "Message": rule["message"]}}, operation)
                if service == "opensearchserverless":
                    return calls.aoss.answer(client, operation, params)
                if service == "lambda" and operation == "Invoke":
                    payload = calls.invoke_answers.pop(0) if calls.invoke_answers else {"ok": True, "detail": "[201, 200]"}
                    return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(payload).encode())}
            return real(client, operation, params)

        patch(context, botocore.client.BaseClient, "_make_api_call", make_api_call)

    def of(self, operation):
        return [p for _, op, p in self.made if op == operation]

    def names(self):
        return [op for _, op, _ in self.made]


class Endpoint:
    """The collection's endpoint for the scripts: indexes with a body and documents; `deny` lists statuses to answer
    to the next requests (401 with the network policy's hint, 403 with the data access policy's)."""

    def __init__(self):
        self.indexes: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.deny: list[int] = []

    def send(self, request):
        import requests
        from urllib.parse import urlparse

        url = urlparse(request.url)
        body = request.body.decode() if isinstance(request.body, bytes) else (request.body or "")
        self.requests.append({"method": request.method, "path": url.path, "body": body,
                              "headers": dict(request.headers)})
        assert len(self.requests) <= REQUEST_LIMIT, "the endpoint stand-in received a loop of requests"
        headers = {"content-type": "application/json"}
        if self.deny:
            status = self.deny.pop(0)
            headers["x-aoss-response-hint"] = "X01:network-policy-deny" if status == 401 else "X01:gw-helper-deny"
            out = {}
        else:
            status, out = self._answer(request.method, url.path, body)
        resp = requests.Response()
        resp.status_code, resp._content = status, json.dumps(out).encode()
        resp.headers.update(headers)
        resp.request, resp.url = request, request.url
        return resp

    def _answer(self, method, path, body):
        parts = [p for p in path.split("/") if p]
        if parts == ["_cat", "indices"]:
            return 200, [{"index": n} for n in self.indexes]
        index = parts[0]
        if len(parts) == 1:
            if method == "HEAD":
                return (200 if index in self.indexes else 404), {}
            if method == "PUT":
                self.indexes[index] = {"body": json.loads(body), "docs": {}}
                return 200, {"acknowledged": True}
            if method == "DELETE":
                self.indexes.pop(index)
                return 200, {"acknowledged": True}
        if parts[1:] == ["_count"]:
            return 200, {"count": len(self.indexes[index]["docs"])}
        if parts[1:] == ["_mapping"]:
            return 200, {index: {"mappings": self.indexes[index]["body"]["mappings"]}}
        raise AssertionError(f"the endpoint stand-in has no answer for {method} {path}")


def use_aws(context) -> Calls:
    """moto for the services it has, Calls for the rest, the endpoint stand-in; waits written down."""
    from moto import mock_aws
    import requests.adapters

    m = mock_aws()
    m.start()
    context.add_cleanup(m.stop)
    context.aoss = Aoss()
    context.calls = Calls(context.aoss)
    context.calls.start(context)
    context.endpoint = Endpoint()
    patch(context, requests.adapters.HTTPAdapter, "send", lambda self, request, **kw: context.endpoint.send(request))
    no_waits(context)
    return context.calls


class UrlIndex:
    """urlopen for main/handler.py: the recipe's StandInIndex behind it, with scripted temporary failures first
    (`fail`: a list of HTTP statuses or "timeout"), and every request written down."""

    def __init__(self, index, fail=()):
        self.index, self.fail, self.requests = index, list(fail), []

    def urlopen(self, req, timeout=None):
        import urllib.error

        self.requests.append({"url": req.full_url, "body": req.data.decode(), "headers": dict(req.header_items()),
                              "timeout": timeout})
        assert len(self.requests) <= REQUEST_LIMIT
        if self.fail:
            what = self.fail.pop(0)
            if what == "timeout":
                raise TimeoutError("the stand-in did not answer")
            raise urllib.error.HTTPError(req.full_url, what, "refused", {}, io.BytesIO(b'{"status": %d}' % what))
        out = self.index.post("/_bulk", req.data.decode())
        return contextlib.nullcontext(io.BytesIO(json.dumps(out).encode()))


def lines_of(text: str) -> list[str]:
    return [x for x in text.splitlines() if x.strip()]


def strip_scores(line: str) -> str:
    return re.sub(r"\(\d+\.\d+\)", "(score)", line)
