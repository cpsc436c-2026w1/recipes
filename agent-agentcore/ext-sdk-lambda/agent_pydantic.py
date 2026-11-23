"""The question with Pydantic AI 2.71.0: the same prompt and tool, the output typed as TripAnswer.

    outcome = await run_pydantic(question, model, tool, s)

`model` is OpenAIChatModel on the course LLM gateway (gateway_model) or a FunctionModel in the tests. Not the model
string "openai:...": in 2.71.0 that builds OpenAIResponsesModel, which calls /v1/responses, and the gateway serves
/v1/chat/completions. The limits: UsageLimits(request_limit, tool_calls_limit) refuse the 5th request and the 4th
run_sql (the output tool is not counted); the loop is driven with agent.iter() and the deadline is checked before
each model request node and inside run_sql; asyncio.timeout_at cancels whatever is in flight at the deadline.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")    # 2.71.0 prints a banner on the first run otherwise

from pydantic_ai import Agent, Tool  # noqa: E402
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded  # noqa: E402
from pydantic_ai.settings import ModelSettings  # noqa: E402
from pydantic_ai.usage import RunUsage, UsageLimits  # noqa: E402

from refusal import gateway_refusal  # noqa: E402
from toollog import DeadlineReached, SqlTool  # noqa: E402
from trip_contracts import RUN_SQL_DESCRIPTION, SYSTEM_PROMPT, TOOL_NAME, TripAnswer  # noqa: E402


def gateway_model(s, token: str):
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(s.agent_model, provider=OpenAIProvider(base_url=s.need("course_gateway_url"),
                                                                  api_key=token))


def build(model, tool: SqlTool, s) -> Agent:
    async def run_sql(sql: str) -> dict:
        # async: it runs on the loop, not in a worker thread, so the deadline check and the log stay in order;
        # DuckDB's own timer (engine.py) is capped at the time left to the deadline.
        return tool(sql)

    return Agent(model, output_type=TripAnswer, system_prompt=SYSTEM_PROMPT,
                 tools=[Tool(run_sql, name=TOOL_NAME, description=RUN_SQL_DESCRIPTION, takes_ctx=False)],
                 retries={"output": 1},
                 model_settings=ModelSettings(max_retries=0, timeout=s.call_timeout_s,
                                              max_tokens=s.reply_max_tokens, temperature=0))


async def run_pydantic(question: str, model, tool: SqlTool, s) -> dict:
    agent = build(model, tool, s)
    usage = RunUsage()          # passed in, so a stopped question still reports its counts
    limits = UsageLimits(request_limit=s.max_model_calls, tool_calls_limit=s.max_tool_calls)
    out = {"output": None, "end": "answered", "detail": ""}
    model_calls, stopped = 0, ""
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout_at(loop.time() + tool.deadline.left()):
            async with agent.iter(question, usage=usage, usage_limits=limits) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        # the node is yielded before it runs: a break here means the call never starts
                        if model_calls >= s.max_model_calls:
                            stopped = f"cap of {s.max_model_calls} model calls"
                            break
                        if tool.deadline.passed():
                            stopped = f"deadline of {tool.deadline.seconds:g} s"
                            break
                        model_calls += 1
                if not stopped:
                    out["output"] = run.result.output
    except TimeoutError:
        stopped = f"deadline of {tool.deadline.seconds:g} s, the call in flight cancelled"
    except DeadlineReached as err:
        stopped = str(err)
    except UsageLimitExceeded as err:
        stopped = str(err).split(". ")[0]
    except UnexpectedModelBehavior as err:
        out["end"], out["detail"] = "output_format", str(err)[:300]
    except ModelAPIError as err:
        refusal = gateway_refusal(err)
        out["end"] = "refused" if refusal else "error"
        out["refusal"], out["detail"] = refusal, f"{type(err).__name__}: {str(err)[:300]}"
    except Exception as err:
        refusal = gateway_refusal(err)
        out["end"] = "refused" if refusal else "error"
        out["refusal"], out["detail"] = refusal, f"{type(err).__name__}: {str(err)[:300]}"
    if stopped:
        out["end"], out["detail"], out["output"] = "stopped", stopped, None
    return {**out, "model_calls": model_calls, "tool_calls": len(tool.log),
            "tokens_in": usage.input_tokens, "tokens_out": usage.output_tokens}
