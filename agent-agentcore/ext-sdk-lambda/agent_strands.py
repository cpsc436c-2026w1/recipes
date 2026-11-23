"""The question with the Strands Agents SDK 1.64.0: one tool, the caps in hooks, the output as TripAnswer.

    outcome = await run_strands(question, model, tool, s)

`model` is Strands' OpenAI provider on the course LLM gateway (gateway_model) or a scripted model in the tests.
The whole agent runs under asyncio.timeout_at of the question's deadline, so a model call that is waiting when
the deadline comes is cancelled (the OpenAI client is an AsyncOpenAI, so its request can be cancelled).
"""
from __future__ import annotations

import asyncio
import json

from strands import Agent
from strands.tools.tools import PythonAgentTool
from strands.types.exceptions import StructuredOutputException

from caps import CallCap, OutputRetry, SqlContract
from refusal import gateway_refusal
from toollog import DeadlineReached, SqlTool
from trip_contracts import RUN_SQL_DESCRIPTION, SYSTEM_PROMPT, TOOL_NAME, RunSqlArgs, TripAnswer


def gateway_model(s, token: str):
    """Strands' OpenAI provider on the gateway. max_retries 0: one model call is one request (agent-agentcore)."""
    from strands.models.openai import OpenAIModel

    return OpenAIModel(client_args={"api_key": token, "base_url": s.need("course_gateway_url"), "max_retries": 0,
                                    "timeout": s.call_timeout_s},
                       model_id=s.agent_model, params={"max_tokens": s.reply_max_tokens, "temperature": 0})


def sql_tool(tool: SqlTool) -> PythonAgentTool:
    schema = RunSqlArgs.model_json_schema()
    schema.pop("title", None)

    def run_tool(tool_use, **kwargs):
        try:
            result = tool(str((tool_use["input"] or {}).get("sql", "")))
        except DeadlineReached as err:
            result = {"error": f"Stopped by the app: {err}."}
        return {"toolUseId": tool_use["toolUseId"], "status": "error" if "error" in result else "success",
                "content": [{"text": json.dumps(result, ensure_ascii=False, default=str)}]}

    run_tool.__name__ = TOOL_NAME
    return PythonAgentTool(TOOL_NAME, {"name": TOOL_NAME, "description": RUN_SQL_DESCRIPTION,
                                       "inputSchema": {"json": schema}}, run_tool)


async def run_strands(question: str, model, tool: SqlTool, s) -> dict:
    """{"output": TripAnswer or None, "end": answered|stopped|output_format|refused|error, ...counts}."""
    cap = CallCap(s.max_model_calls, s.max_tool_calls, tool.deadline)
    output_retry = OutputRetry(retries=1)
    agent = Agent(name="trip_agent", model=model, system_prompt=SYSTEM_PROMPT, callback_handler=None,
                  tools=[sql_tool(tool)],
                  # No retries by the SDK: its default makes 6 attempts with waits of 4 s, 8 s and longer.
                  retry_strategy=None,
                  hooks=[output_retry, cap, SqlContract()])
    out = {"output": None, "end": "answered", "detail": ""}
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout_at(loop.time() + tool.deadline.left()):
            result = await agent.invoke_async(question, structured_output_model=TripAnswer)
        out["output"] = result.structured_output
    except TimeoutError:
        cap.stopped = cap.stopped or f"deadline of {tool.deadline.seconds:g} s, the call in flight cancelled"
    except DeadlineReached as err:
        cap.stopped = cap.stopped or str(err)
    except StructuredOutputException as err:
        out["end"], out["detail"] = "output_format", str(err)[:300]
    except Exception as err:                   # whatever the model client raised: the question ends here
        refusal = gateway_refusal(err)
        out["end"] = "refused" if refusal else "error"
        out["refusal"], out["detail"] = refusal, f"{type(err).__name__}: {str(err)[:300]}"
    if output_retry.failed:
        out["end"], out["output"] = "output_format", None
    elif cap.stopped and out["end"] in ("answered", "output_format"):
        out["end"], out["detail"], out["output"] = "stopped", cap.stopped, None
    if out["end"] == "answered" and out["output"] is None:
        out["end"] = "output_format"
    usage = agent.event_loop_metrics.accumulated_usage     # summed over the calls that returned their usage
    return {**out, "model_calls": cap.model_calls, "tool_calls": len(tool.log),
            "tokens_in": usage.get("inputTokens", 0), "tokens_out": usage.get("outputTokens", 0)}
