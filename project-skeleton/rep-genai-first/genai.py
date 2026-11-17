"""Generative AI first: a question -> course LLM gateway -> a model that reads DynamoDB through one read-only tool ->
a reply that states the number.

The store is rep-operational-first's DynamoDB table. The assistant answers from the table's current state (the
per-minute counters), not from the lake: the lake is as fresh as its last export. The same question asked of the lake
is `lake_example.py`. Read with the README of the recipe, section Decisions; each "Decision:" comment names its row.

Vocabulary (as in L15 to L17): system prompt, user message, tool call, tool result, reply.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from skeleton.config import REGION
from skeleton.events import iso

DEFAULT_MODEL = "qwen3.6-35b-a3b"    # the cheaper of the course gateway's two chat models (course price table)
MAX_STEPS = 3                        # Decision "bounded loop": at most 3 model calls per question
MAX_TOKENS = 400                     # per model call; the reply is one sentence
# Decision "bounded loop": a model that reasons before it answers spends part of max_tokens on the reasoning, so it gets
# more room. With 150, qwen3.8-27b's replies came back empty (finish_reason "length"); with 600, one answer of two
# sentences took 330 completion tokens, 277 of them reasoning
# (../../hybrid-retrieval-eval/output/routing-gateway-150-cap.txt).
MAX_TOKENS_BY_MODEL = {"qwen3.8-27b": 600}
CAP_USED_UP = "the model used its token cap before answering"
MAX_WINDOW_MINUTES = 24 * 60         # ours: the tool reads at most one day of counters (15 BatchGetItem calls)
BATCH_KEYS = 100                     # BatchGetItem takes at most 100 keys per call (DynamoDB docs)
MAX_READS = 5                        # as GET /number (R58): the same counters are asked for at most 5 times

TOOLS = [{
    "type": "function",
    "function": {
        "name": "count_updates",
        "description": "Count the updates whose event time is in [start, end), from the operational table. "
                       "Times are UTC whole minutes, for example 2026-11-12T15:00:00.000Z.",
        "parameters": {"type": "object",
                       "properties": {"start": {"type": "string", "description": "first minute, included"},
                                      "end": {"type": "string", "description": "last minute, not included"}},
                       "required": ["start", "end"]},
    },
}]


def max_tokens_for(model: str) -> int:
    return MAX_TOKENS_BY_MODEL.get(model, MAX_TOKENS)


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


def system_prompt(start: datetime, end: datetime) -> str:
    """The system prompt. Decision "context": what the tool counts and the exact window, so the model does not guess
    the time."""
    return (
        "You answer questions about live updates from bike-share stations. Use the tool count_updates to read the "
        "operational table; do not answer a number you did not read.\n"
        "count_updates counts distinct updates by event time (UTC) over whole minutes; copies are already removed.\n"
        f"'The last hour' means start '{iso(start)}' and end '{iso(end)}'.\n"
        "Reply in one sentence that states the number."
    )


def _minute(t: str) -> datetime:
    """A tool argument as a time. Accepts 2026-11-12T15:00:00.000Z or 2026-11-12T15:00Z; must be a whole minute."""
    if not isinstance(t, str) or not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d{1,3})?)?Z$", t):
        raise ValueError(f"time {t!r}: use the form 2026-11-12T15:00:00.000Z")
    d = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc)
    if d.second or d.microsecond:
        raise ValueError(f"time {t!r}: use whole minutes")
    return d


def count_updates(ddb, table: str, start: str, end: str, sleep=time.sleep) -> dict:
    """Decision "read-only tool": the tool's only DynamoDB action is BatchGetItem on the minute counters.

    The checks here are the first guard (whole minutes, end after start, at most a day). The permission boundary,
    a role that holds only `tool_policy()`, is the guard that holds when code changes.
    """
    s, e = _minute(start), _minute(end)
    if not s < e:
        raise ValueError("end must be after start")
    minutes = int((e - s).total_seconds() // 60)
    if minutes > MAX_WINDOW_MINUTES:
        raise ValueError(f"at most {MAX_WINDOW_MINUTES} minutes per call")
    keys = [{"pk": {"S": f"minute#{(s + timedelta(minutes=i)):%Y-%m-%dT%H:%M}"}} for i in range(minutes)]
    total = 0
    for i in range(0, len(keys), BATCH_KEYS):
        # Decision "reads": eventually consistent, half the read units of a strongly consistent read.
        request, tries = {table: {"Keys": keys[i:i + BATCH_KEYS], "ProjectionExpression": "n"}}, 0
        while request:
            r = ddb.batch_get_item(RequestItems=request)
            total += sum(int(it["n"]["N"]) for it in r["Responses"].get(table, []))
            request = r.get("UnprocessedKeys") or {}
            if request:
                tries += 1
                if tries == MAX_READS:
                    # Limit the retries (REL05-BP03). ask() hands this error to the model as the tool result.
                    raise RuntimeError("the table is busy: counters were left unread 5 times; ask again in a moment")
                sleep(min(0.05 * 2 ** tries, 1))
    return {"updates": total, "start": iso(s), "end": iso(e)}


def tool_policy(table: str, account: str) -> dict:
    """The IAM policy for whatever runs the tool in a deployed app: one action on one table, nothing else.

    In this recipe the tool runs on your laptop with your own credentials; give a deployed assistant a role with
    only this policy.
    """
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:BatchGetItem"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{table}"}]}


def ask(question: str, client, model: str, tool: Callable[[str, str], dict], start: datetime, end: datetime,
        max_steps: int = MAX_STEPS) -> dict:
    """One question, at most `max_steps` model calls. Returns the reply, the tool calls and the tool results."""
    messages = [{"role": "system", "content": system_prompt(start, end)},
                {"role": "user", "content": question}]
    trace = {"calls": [], "results": [], "steps": 0, "tokens_in": 0, "tokens_out": 0}
    for _ in range(max_steps):
        r = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, **sampling(model),
                                           max_tokens=max_tokens_for(model))
        trace["steps"] += 1
        if getattr(r, "usage", None):
            trace["tokens_in"] += r.usage.prompt_tokens or 0
            trace["tokens_out"] += r.usage.completion_tokens or 0
        msg = r.choices[0].message
        if getattr(r.choices[0], "finish_reason", None) == "length" and _cut(msg):
            # Decision "check the reply": the reply stopped at max_tokens before it was whole. Not an answer and not a
            # refusal: the run reports it as an error, runs no cut tool call and does not check it as a reply.
            return {"reply": "", "error": CAP_USED_UP, **trace}
        if not msg.tool_calls:
            return {"reply": msg.content or "", **trace}
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for call in msg.tool_calls:          # each tool call gets a tool result, even a refused one
            try:
                if call.function.name != "count_updates":
                    raise ValueError(f"no tool named {call.function.name}")
                args = json.loads(call.function.arguments)
                result = tool(args.get("start"), args.get("end"))
                trace["calls"].append(args)
                trace["results"].append(result)
                content = json.dumps(result)
            except Exception as e:           # the model sees the error and may try again within the step limit
                content = json.dumps({"error": str(e)})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return {"reply": f"Stopped after {max_steps} model calls without a reply.", **trace}


def _cut(msg) -> bool:
    """No text and no tool call, or a tool call whose arguments are not whole JSON (cut at the cap)."""
    if not msg.tool_calls:
        return not (msg.content or "").strip()
    for call in msg.tool_calls:
        try:
            json.loads(call.function.arguments or "")
        except json.JSONDecodeError:
            return True
    return False


def grounded(answer: dict) -> bool:
    """Decision "check the reply": the number in the reply must be a count the tool returned."""
    said = set(re.findall(r"\d+", answer["reply"].replace(",", "")))
    read = {str(r["updates"]) for r in answer["results"]}
    return bool(said & read)


def gateway_env() -> tuple[str, str]:
    """The gateway's address and your token from the environment: COURSE_GATEWAY_URL, else GATEWAY_URL; KB_MCP_TOKEN,
    else COURSE_MCP_TOKEN (the older names still work). Never from a file: a .env file is not read."""
    url = os.environ.get("COURSE_GATEWAY_URL") or os.environ.get("GATEWAY_URL") or ""
    token = os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN") or ""
    return url.rstrip("/"), token


def gateway_client():
    """The course LLM gateway: an OpenAI-compatible endpoint. The token is your course MCP token, from the environment."""
    from openai import OpenAI
    url, token = gateway_env()
    if not url or not token:
        raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN (see the README, Run it), or use --llm stub")
    # 250 s: the gateway waits up to 240 s for a thinking model, and a reply that is not streamed sends no byte until
    # the model has finished.
    return OpenAI(base_url=url, api_key=token, timeout=250, max_retries=2)
