"""The example tools through an AgentCore Gateway: an MCP endpoint with AWS_IAM inbound authorization.

The Gateway turns the tools Lambda into MCP tools named `<target>___<tool>` (library-tools___search_books).
A caller signs each request with its AWS credentials (SigV4, service name bedrock-agentcore) and needs
`bedrock-agentcore:InvokeGateway` on the Gateway: the Runtime's execution role inside AgentCore, your own profile
on the laptop.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from main.config import TARGET_NAME
from main.contracts import TOOLS, parse_result, short_name


class GatewaySigV4(httpx.Auth):
    """Sign every request to the Gateway with the caller's AWS credentials."""
    requires_request_body = True

    def __init__(self, session: boto3.Session, region: str):
        self.creds, self.region = session.get_credentials(), region

    def auth_flow(self, request: httpx.Request):
        body = request.content or b""
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=body,
                             headers={"content-type": request.headers.get("content-type", "application/json")})
        SigV4Auth(self.creds.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        request.headers["x-amz-content-sha256"] = hashlib.sha256(body).hexdigest()
        yield request


def open_gateway(url: str, region: str):
    """A started Strands MCP client for the Gateway. Close it with client.stop(None, None, None)."""
    from strands.tools.mcp import MCPClient

    client = MCPClient(url=url, auth_provider=GatewaySigV4(boto3.Session(region_name=region), region))
    client.start()
    return client


class GatewayLibrary:
    """What app code needs from the library, read through the Gateway's tools: the Strands tools for the agent,
    and get_book / get_holds for the approval hook and the run report. `client` is a started MCP client."""

    def __init__(self, client):
        self.client = client
        # short name -> the MCP tool, for this recipe's target only (a Gateway can hold other targets)
        self.by_name = {short_name(t.tool_name): t for t in client.list_tools_sync()
                        if t.tool_name.startswith(f"{TARGET_NAME}___") and short_name(t.tool_name) in TOOLS}
        missing = sorted(set(TOOLS) - set(self.by_name))
        if missing:
            raise RuntimeError(f"the Gateway does not list {', '.join(missing)}: is the target {TARGET_NAME} READY?")

    @property
    def tools(self) -> list:
        return list(self.by_name.values())

    def _call(self, name: str, arguments: dict) -> dict:
        result = self.client.call_tool_sync(uuid.uuid4().hex, self.by_name[name].tool_name, arguments)
        text = " ".join(c.get("text", "") for c in result.get("content", []))
        try:
            found = json.loads(text)
        except json.JSONDecodeError:
            return {"error": text[:200] or "empty tool result"}
        if not isinstance(found, dict):
            return {"error": text[:200]}
        return parse_result(name, found).model_dump()     # the result must have the contract's shape

    def get_book(self, book_id: str) -> dict:
        return self._call("get_book", {"book_id": book_id})

    def get_holds(self, member: str) -> dict:
        return self._call("get_holds", {"member": member})
