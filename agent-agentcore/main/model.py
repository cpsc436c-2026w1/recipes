"""The two models the agent can run on: the course LLM gateway, and a stub with fixed behaviour for offline runs.

`gateway_model()` is Strands' OpenAI provider with the gateway's address (Strands docs, "OpenAI": client_args
with api_key and base_url reach any OpenAI-compatible server). The gateway checks the key, the per-key budget
and the rate limit before it forwards a call; that budget lives outside this app, so a loop here cannot remove it.

`StubModel` is a Strands model that makes no network call. It reads the conversation so far and returns the next
tool call or the reply, the way its `behaviour` says. The tests, the behave scenarios and the offline eval run on it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from strands.models import Model

from main.config import Settings
from main.contracts import short_name

BEHAVIOURS = ("careful", "runaway", "wrong_member", "asks_twice", "wrong_arguments", "obeys_book_text")


def sampling(model: str) -> dict:
    """Temperature 0 (repeatable replies) for every model except OpenAI GPT and o-series models, which accept only
    their default (1): sent 0, the gateway answers 400 upstream_rejected. They get no temperature at all."""
    return {} if re.match(r"(openai/)?(gpt-|o\d)", model.lower()) else {"temperature": 0}


def gateway_model(s: Settings):
    """The agent's model on the course LLM gateway. Stops with the variable's name when the address or token is empty."""
    from strands.models.openai import OpenAIModel

    # src: receipt: https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/openai/ read 2026-11-16
    # max_retries 0: the OpenAI client would send a refused request again by itself (two more times), inside one
    # model call that the cap counted once. One model call is then one request to the gateway.
    # src: measured: tests/test_gateway_model.py, a stand-in that answers 500 got 3 requests for 1 model call, 2026-11-16
    return OpenAIModel(client_args={"api_key": s.need("kb_mcp_token"), "base_url": s.need("course_gateway_url"),
                                    "max_retries": 0},
                       model_id=s.agent_model, params={"max_tokens": s.reply_max_tokens, **sampling(s.agent_model)})


def _results(messages) -> list[tuple[str, str, Any]]:
    """(tool, status, result) for every tool result in the conversation, oldest first."""
    names, out = {}, []
    for message in messages:
        for block in message["content"]:
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = short_name(block["toolUse"]["name"])
            if "toolResult" in block:
                r = block["toolResult"]
                text = " ".join(c.get("text", "") for c in r.get("content", []))
                try:
                    found = json.loads(text)
                except json.JSONDecodeError:
                    found = {"error": text}
                out.append((names.get(r["toolUseId"], "?"), r.get("status", "success"), found))
    return out


def _request(messages) -> tuple[str, str]:
    """(member, request) from the first user message, "Member sam: Which is shorter, ...?"."""
    text = next(b["text"] for b in messages[0]["content"] if "text" in b)
    m = re.match(r"Member (\w+): (.*)", text, flags=re.S)
    return (m.group(1), m.group(2)) if m else ("", text)


class StubModel(Model):
    """A model with fixed behaviour. `calls` counts the model calls it answered.

    careful           searches what the request names, places a hold on the shortest book when the request asks
    runaway           asks for another search on every call and never replies
    wrong_member      careful, but writes for member "alex" in place of the member of the request
    asks_twice        careful, but asks for the same write a second time after the first went through
    wrong_arguments   careful, but its first search has an argument the tool does not have
    obeys_book_text   reads book b09, whose summary holds an instruction, and tries to place a hold on it
    """

    def __init__(self, behaviour: str = "careful"):
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"behaviour must be one of {BEHAVIOURS}")
        self.behaviour, self.calls = behaviour, 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict:
        return {"model_id": f"stub-{self.behaviour}"}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("the stub model has no structured output")

    # ---- what the stub decides -------------------------------------------------------------------------------
    def next_step(self, messages) -> dict:
        """{"tool": name, "arguments": {...}} or {"reply": text}."""
        member, request = _request(messages)
        results = _results(messages)
        if self.behaviour == "runaway":
            return {"tool": "search_books", "arguments": {"query": f"novel {len(results)}"}}
        if self.behaviour == "obeys_book_text":
            steps = [{"tool": "get_book", "arguments": {"book_id": "b09"}},
                     {"tool": "place_hold", "arguments": {"member": member, "book_id": "b09"}}]
        else:
            steps = self._plan(member, request, results)
        return steps[len(results)] if len(results) < len(steps) else {"reply": _summary(results)}

    def _plan(self, member: str, request: str, results) -> list[dict]:
        text = request.lower()
        steps: list[dict] = []
        if self.behaviour == "wrong_arguments":
            steps.append({"tool": "search_books", "arguments": {"q": "novel"}})
        pair = re.search(r"(\w+) or (\w+)", text)
        named = re.search(r"shortest ([a-z ]+?)(?: book| to | on |[.?!,]|$)", text)
        book = re.search(r"book (b\d+)", text)
        queries = list(pair.groups()) if pair else [named.group(1).strip()] if named else []
        steps += [{"tool": "search_books", "arguments": {"query": q}} for q in queries]
        if "place a hold" in text:
            found = [min(r["books"], key=lambda b: b["pages"]) for tool, _, r in results
                     if tool == "search_books" and r.get("books")]
            target = book.group(1) if book else min(found, key=lambda b: b["pages"])["book_id"] if found else ""
            to = "alex" if self.behaviour == "wrong_member" else member
            write = {"tool": "place_hold", "arguments": {"member": to, "book_id": target}}
            steps += [write, write] if self.behaviour == "asks_twice" else [write]
        elif "my holds" in text:
            steps.append({"tool": "get_holds", "arguments": {"member": member}})
        elif book:
            steps.append({"tool": "get_book", "arguments": {"book_id": book.group(1)}})
        return steps

    # ---- the Strands side: one model call as a stream of events -----------------------------------------------
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        step = self.next_step(messages)
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in step:
            # the tool's full name: the Gateway's tools are listed as <target>___<tool>
            full = next((t["name"] for t in tool_specs or [] if short_name(t["name"]) == step["tool"]), step["tool"])
            yield {"contentBlockStart": {"start": {"toolUse": {"name": full, "toolUseId": f"stub-{self.calls}"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step["arguments"])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": step["reply"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                            "metrics": {"latencyMs": 0}}}


def _summary(results) -> str:
    """The stub's one-sentence reply, from the tool results it read."""
    placed = [r for tool, status, r in results if tool == "place_hold" and status == "success" and "title" in r]
    refused = [r for tool, status, r in results if tool == "place_hold" and status != "success"]
    holds = [r for tool, _, r in results if tool == "get_holds" and "holds" in r]
    found = [b for tool, _, r in results if tool == "search_books" for b in r.get("books", [])]
    if placed:
        return f"I placed a hold on {placed[-1]['title']} for you."
    if refused:
        return "No hold was placed."
    if holds:
        return f"You have {len(holds[-1]['holds'])} books on hold."
    read = [r for tool, _, r in results if tool == "get_book" and "title" in r]
    if read:
        return f"{read[-1]['title']} by {read[-1]['author']} has {read[-1]['pages']} pages."
    if found:
        shortest = min(found, key=lambda b: b["pages"])
        return f"{shortest['title']} is the shortest, at {shortest['pages']} pages."
    return "I could not find that."
