"""The Lambda function `<PREFIX>-agent-sdk`: one analyst question in, one JSON answer with a status out.

    POST <function URL>  {"question": "How many trips were requested on 2024-03-14?"}     (signed with SigV4)
    -> {"status": "answered", "reply": ..., "value": 19074, "sql": ..., "rows": [...], "model_calls": 2, ...}

Statuses: `answered` (value, SQL and rows from the tool log), `did_not_answer` (reason no_value,
value_not_in_result, reply_disagrees, output_format, or a refusal of the gateway with its status and code),
`stopped` (a cap or the deadline, with the counts of the calls that ran), `error` (anything else). Only `answered`
carries a value.

`AGENT_SDK` chooses the loop: `strands` (agent_strands.py) or `pydantic-ai` (agent_pydantic.py). The settings, the
DuckDB engine over trips.parquet and the course token are made once per execution environment.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.append(str(HERE))

from engine import Engine  # noqa: E402
from settings import Settings, load_settings  # noqa: E402
from toollog import Deadline, SqlTool  # noqa: E402
from verdict import verdict  # noqa: E402

_CACHE: dict = {}
FIELDS = ("status", "reason", "reply", "value", "derived", "sql", "rows", "model_calls", "tool_calls", "http_status", "code",
          "retry_after", "sdk", "seconds")


def settings() -> Settings:
    if "settings" not in _CACHE:
        os.environ.setdefault("KB_MCP_TOKEN", "")      # in Lambda the token is in Parameter Store, not the environment
        _CACHE["settings"] = load_settings()
    return _CACHE["settings"]


def engine(s: Settings) -> Engine:
    if "engine" not in _CACHE:
        path = Path(s.trips_path)
        _CACHE["engine"] = Engine(path if path.is_absolute() else HERE / path)
    return _CACHE["engine"]


def token(s: Settings) -> str:
    """The course token: KB_MCP_TOKEN when set (a laptop run), else the SecureString parameter, read once."""
    if s.kb_mcp_token:
        return s.kb_mcp_token
    if "token" not in _CACHE:
        import boto3

        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", s.aws_region))
        _CACHE["token"] = ssm.get_parameter(Name=s.need("kb_mcp_token_param"), WithDecryption=True)["Parameter"]["Value"]
    return _CACHE["token"]


def model_for(s: Settings, sdk: str):
    if sdk == "strands":
        from agent_strands import gateway_model
    else:
        from agent_pydantic import gateway_model
    return gateway_model(s, token(s))


async def answer_async(question: str, s: Settings, *, sdk: str | None = None, model=None, eng: Engine | None = None,
                       clock=time.monotonic) -> dict:
    sdk = sdk or s.agent_sdk
    tool = SqlTool(eng or engine(s), Deadline(s.deadline_s, clock=clock))
    model = model if model is not None else model_for(s, sdk)
    if sdk == "strands":
        from agent_strands import run_strands as run
    else:
        from agent_pydantic import run_pydantic as run
    outcome = await run(question, model, tool, s)
    out = {"status": "error", "reason": None, "reply": None, "value": None, "sdk": sdk,
           "model_calls": outcome["model_calls"], "tool_calls": outcome["tool_calls"]}
    end = outcome["end"]
    if end == "answered":
        out.update(verdict(outcome["output"], question, tool.log))
    elif end == "stopped":
        out.update(status="stopped", reason=outcome["detail"])
    elif end == "output_format":
        out.update(status="did_not_answer", reason="output_format")
    elif end == "refused":
        out.update(status="did_not_answer", reason="gateway_refused", **outcome["refusal"])
    else:
        out.update(reason=outcome["detail"])
    out["seconds"] = round(tool.deadline.elapsed(), 3)
    # The model's tokens go to the function's log, not to the analyst: the cost of a question (README, Cost)
    out["tokens_in"], out["tokens_out"] = outcome.get("tokens_in", 0), outcome.get("tokens_out", 0)
    out["log"] = tool.log
    return out


def answer(question: str, s: Settings | None = None, **kw) -> dict:
    return asyncio.run(answer_async(question, s or settings(), **kw))


def public(result: dict) -> dict:
    """What the analyst gets: the fields above (the tool log stays in the function's log)."""
    return {k: result.get(k) for k in FIELDS}


def handler(event, context):
    """Function URL event (payload format 2.0). The URL's auth type AWS_IAM refuses an unsigned request before
    this runs."""
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        question = str(json.loads(body).get("question", "")).strip()
    except (ValueError, AttributeError):
        question = ""
    if not question:
        return _http(400, {"status": "error", "reason": 'send {"question": "..."}'})
    result = answer(question)
    print(json.dumps({"question": question, **public(result), "tokens_in": result["tokens_in"],
                      "tokens_out": result["tokens_out"], "log": result["log"]}, default=str))
    return _http(200, public(result))


def _http(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(body, default=str)}
