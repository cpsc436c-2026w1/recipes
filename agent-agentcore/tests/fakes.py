"""A stand-in for the AgentCore control API (moto does not have it), and stand-ins for a person and a Lambda context.

`FakeAgentCore` keeps gateways, targets and runtimes in memory. Every call's parameters are checked against the
real service model that ships with botocore: required members, types, enum values and the name patterns
(botocore's own validation does not check patterns). A resource is CREATING on the first look and READY on the
second, and a deleted one is DELETING for one look, so the waits in main/infra.py and clean_up.py are exercised.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import ParamValidator

ACCOUNT = "123456789012"
REGION = "ca-central-1"
MODEL = boto3.client("bedrock-agentcore-control", region_name=REGION, aws_access_key_id="testing",
                     aws_secret_access_key="testing").meta.service_model


def check_patterns(value, shape, path="") -> list[str]:
    """Every string in `value` against the pattern and the enum its shape declares."""
    problems = []
    if shape.type_name == "structure" and isinstance(value, dict):
        for name, member in shape.members.items():
            if name in value:
                problems += check_patterns(value[name], member, f"{path}.{name}")
    elif shape.type_name == "list" and isinstance(value, list):
        for i, item in enumerate(value):
            problems += check_patterns(item, shape.member, f"{path}[{i}]")
    elif shape.type_name == "map" and isinstance(value, dict):
        for key, item in value.items():
            problems += check_patterns(item, shape.value, f"{path}.{key}")
    elif shape.type_name == "string" and isinstance(value, str):
        pattern = shape.metadata.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            problems.append(f"{path}: {value!r} does not match {pattern}")
        if getattr(shape, "enum", None) and value not in shape.enum:
            problems.append(f"{path}: {value!r} is not one of {shape.enum}")
    return problems


def error(code: str, operation: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


class FakeAgentCore:
    def __init__(self):
        self.gateways: dict[str, dict] = {}
        self.targets: dict[tuple[str, str], dict] = {}
        self.runtimes: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.refuse: set[str] = set()         # operation names that answer AccessDeniedException
        self._n = 0

    def _check(self, operation: str, params: dict) -> None:
        shape = MODEL.operation_model(operation).input_shape
        report = ParamValidator().validate(params, shape)
        problems = check_patterns(params, shape)
        if report.has_errors() or problems:
            raise ParamValidationError(report="; ".join(filter(None, [report.generate_report(), *problems])))
        self.calls.append((operation, params))
        if operation in self.refuse:
            raise error("AccessDeniedException", operation, "not allowed in this test")

    def _id(self, name: str) -> str:
        self._n += 1
        return f"{name.lower()}-{self._n:010d}"

    @staticmethod
    def _look(item: dict) -> str:
        """The status of one look: CREATING or UPDATING once, then READY."""
        status = item["status"]
        if status in ("CREATING", "UPDATING"):
            item["status"] = "READY"
        return status

    def get_paginator(self, name: str):
        return SimpleNamespace(paginate=lambda **kw: [getattr(self, name)(**kw)])

    # ---- gateways ----
    def create_gateway(self, **kw):
        self._check("CreateGateway", kw)
        if any(g["name"] == kw["name"] for g in self.gateways.values()):
            raise error("ConflictException", "CreateGateway")
        gid = self._id(kw["name"])
        self.gateways[gid] = {**kw, "gatewayId": gid, "status": "CREATING",
                              "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gid}",
                              "gatewayUrl": f"https://{gid}.gateway.test.invalid/mcp"}
        return dict(self.gateways[gid])

    def list_gateways(self, **kw):
        self._check("ListGateways", kw)
        return {"items": [{"gatewayId": g["gatewayId"], "name": g["name"], "status": g["status"]}
                          for g in self.gateways.values()]}

    def get_gateway(self, **kw):
        self._check("GetGateway", kw)
        g = self.gateways.get(kw["gatewayIdentifier"])
        if g is None:
            raise error("ResourceNotFoundException", "GetGateway")
        return {**g, "status": self._look(g)}

    def delete_gateway(self, **kw):
        self._check("DeleteGateway", kw)
        gid = kw["gatewayIdentifier"]
        if gid not in self.gateways:
            raise error("ResourceNotFoundException", "DeleteGateway")
        if any(g == gid for g, _ in self.targets):
            raise error("ConflictException", "DeleteGateway", "the gateway still has targets")
        del self.gateways[gid]
        return {"gatewayId": gid, "status": "DELETING"}

    # ---- targets ----
    def create_gateway_target(self, **kw):
        self._check("CreateGatewayTarget", kw)
        if kw["gatewayIdentifier"] not in self.gateways:
            raise error("ResourceNotFoundException", "CreateGatewayTarget")
        self._n += 1
        tid = f"T{self._n:09d}"
        self.targets[(kw["gatewayIdentifier"], tid)] = {**kw, "targetId": tid, "status": "CREATING"}
        return {"targetId": tid, "status": "CREATING"}

    def update_gateway_target(self, **kw):
        self._check("UpdateGatewayTarget", kw)
        key = (kw["gatewayIdentifier"], kw["targetId"])
        if key not in self.targets:
            raise error("ResourceNotFoundException", "UpdateGatewayTarget")
        self.targets[key].update({**kw, "status": "UPDATING"})
        return {"targetId": kw["targetId"], "status": "UPDATING"}

    def list_gateway_targets(self, **kw):
        self._check("ListGatewayTargets", kw)
        return {"items": [{"targetId": t["targetId"], "name": t["name"], "status": t["status"]}
                          for (g, _), t in self.targets.items() if g == kw["gatewayIdentifier"]]}

    def get_gateway_target(self, **kw):
        self._check("GetGatewayTarget", kw)
        key = (kw["gatewayIdentifier"], kw["targetId"])
        t = self.targets.get(key)
        if t is None:
            raise error("ResourceNotFoundException", "GetGatewayTarget")
        if t["status"] == "DELETING":       # seen once as DELETING, then gone
            del self.targets[key]
            return {**t}
        return {**t, "status": self._look(t)}

    def delete_gateway_target(self, **kw):
        self._check("DeleteGatewayTarget", kw)
        key = (kw["gatewayIdentifier"], kw["targetId"])
        if key not in self.targets:
            raise error("ResourceNotFoundException", "DeleteGatewayTarget")
        self.targets[key]["status"] = "DELETING"
        return {"targetId": kw["targetId"], "status": "DELETING"}

    # ---- runtimes ----
    def create_agent_runtime(self, **kw):
        self._check("CreateAgentRuntime", kw)
        if any(r["agentRuntimeName"] == kw["agentRuntimeName"] for r in self.runtimes.values()):
            raise error("ConflictException", "CreateAgentRuntime")
        self._n += 1
        rid = f"{kw['agentRuntimeName']}-{self._n:010d}"
        self.runtimes[rid] = {**kw, "agentRuntimeId": rid, "status": "CREATING", "agentRuntimeVersion": "1",
                              "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{rid}"}
        return dict(self.runtimes[rid])

    def update_agent_runtime(self, **kw):
        self._check("UpdateAgentRuntime", kw)
        r = self.runtimes.get(kw["agentRuntimeId"])
        if r is None:
            raise error("ResourceNotFoundException", "UpdateAgentRuntime")
        r.update({**kw, "status": "UPDATING", "agentRuntimeVersion": str(int(r["agentRuntimeVersion"]) + 1)})
        return dict(r)

    def list_agent_runtimes(self, **kw):
        self._check("ListAgentRuntimes", kw)
        return {"agentRuntimes": [{k: r[k] for k in ("agentRuntimeId", "agentRuntimeArn", "agentRuntimeName", "status")}
                                  for r in self.runtimes.values()]}

    def get_agent_runtime(self, **kw):
        self._check("GetAgentRuntime", kw)
        r = self.runtimes.get(kw["agentRuntimeId"])
        if r is None:
            raise error("ResourceNotFoundException", "GetAgentRuntime")
        if r["status"] == "DELETING":
            del self.runtimes[kw["agentRuntimeId"]]
            return dict(r)
        return {**r, "status": self._look(r)}

    def delete_agent_runtime(self, **kw):
        self._check("DeleteAgentRuntime", kw)
        r = self.runtimes.get(kw["agentRuntimeId"])
        if r is None:
            raise error("ResourceNotFoundException", "DeleteAgentRuntime")
        r["status"] = "DELETING"
        return {"status": "DELETING"}


def lambda_context(tool_name: str):
    """The context the Gateway gives the tools function: the tool's name, with the target's, in the client context."""
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool_name}))


def tool_event(name: str, call_id: str, **arguments):
    """A stand-in for Strands' BeforeToolCallEvent, for calling a hook's callback without a model."""
    return SimpleNamespace(tool_use={"name": name, "toolUseId": call_id, "input": arguments}, cancel_tool=False,
                           cancel_message=None, result=None)


class FakeGatewayClient:
    """A stand-in for the started MCP client of the Gateway: it lists the tools under the names the Gateway gives
    them, `<target>___<tool>`, and runs a call on the local tools. `extra` adds tools of another target."""

    def __init__(self, library, target: str = "library-tools", extra: tuple = ()):
        from main.agent import local_tools

        self.library, self.calls = library, []
        self.listed = []
        for tool in local_tools(library):
            spec = {**tool.tool_spec, "name": f"{target}___{tool.tool_name}"}
            self.listed.append(type(tool)(spec["name"], spec, self._runner(tool.tool_name)))
        self.listed += [SimpleNamespace(tool_name=name) for name in extra]

    def _runner(self, name: str):
        def run_tool(tool_use, **kwargs):
            return self.call_tool_sync(tool_use["toolUseId"], f"x___{name}", dict(tool_use["input"] or {}))
        run_tool.__name__ = name
        return run_tool

    def list_tools_sync(self):
        return list(self.listed)

    def call_tool_sync(self, call_id, name, arguments):
        import json

        short = name.split("___")[-1]
        self.calls.append((short, arguments))
        result = self.library.call(short, arguments)
        return {"toolUseId": call_id, "status": "error" if "error" in result else "success",
                "content": [{"text": json.dumps(result)}]}
