"""The tools Lambda behind the AgentCore Gateway: the four library tools over two DynamoDB tables.

The Gateway speaks MCP to the agent (tools/list, tools/call). This function does not: it receives the tool's
arguments as `event`, and the tool's name, prefixed with the target's name, in the client context
(AgentCore docs, "AWS Lambda function targets", Lambda function input format).
src: receipt: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html read 2026-11-16

The zip holds this file, store.py and tools.py: standard library and boto3 only. The settings are the
environment variables the deploy step sets; there is no .env in Lambda.
"""
import os

from store import DynamoStore
from tools import LibraryTools

_library = None


def load() -> dict:
    """The function's settings. A missing one fails the first invocation with the variable's name."""
    return {"region": os.environ["AWS_REGION"], "books_table": os.environ["BOOKS_TABLE"],
            "holds_table": os.environ["HOLDS_TABLE"]}


def library() -> LibraryTools:
    global _library
    if _library is None:        # once per execution environment, reused by warm invocations
        cfg = load()
        _library = LibraryTools(DynamoStore(cfg["books_table"], cfg["holds_table"], cfg["region"]))
    return _library


def lambda_handler(event, context):
    custom = context.client_context.custom if context.client_context else {}
    full = (custom or {}).get("bedrockAgentCoreToolName", "")
    name = full.split("___", 1)[1] if "___" in full else full
    try:
        return library().call(name, dict(event or {}))
    except Exception as e:      # the reason goes back to the agent as the tool result
        return {"error": f"{type(e).__name__}: {e}"}
