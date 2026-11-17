"""Inspect task: a few member requests, each run by the agent of main/agent.py and scored in code (scoring.py).

    uv run --env-file .env.example inspect eval evals/task.py                      offline, on the stub model
    uv run --env-file .env inspect eval evals/task.py -T backend=gateway           live, on the course LLM gateway
    uv run inspect view

The system under test is the whole agent (model, tools, hooks), so the solver runs the agent and Inspect's own
model is not called: the task names Inspect's built-in mock model. With backend=stub the agent's model is
StubModel, and a sample may name the stub's behaviour ("stub": "runaway"): those samples show that a hook, not
the model, decides the outcome. With backend=gateway the samples that name a behaviour are left out, and each of
the others makes at most MAX_MODEL_CALLS model calls.

Two scores per request: `outcome` (1 when scoring.check finds no problem) and `model_calls` (its mean is reported).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))      # `inspect eval evals/task.py` loads this file by path: make main/ importable

from inspect_ai import Task, task  # noqa: E402
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Target, accuracy, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from evals import scoring
from main.agent import run_request
from main.config import Settings, load_settings
from main.store import MemoryStore
from main.tools import LibraryTools

BACKENDS = ("stub", "gateway")


def samples(backend: str) -> list[Sample]:
    rows = [json.loads(line) for line in (HERE / "requests.jsonl").read_text().splitlines() if line.strip()]
    if backend == "gateway":
        rows = [r for r in rows if "stub" not in r]          # a real model has no behaviour to pick
    return [Sample(id=r["id"], input=r["input"], target=json.dumps(r["expect"]), metadata=r) for r in rows]


def one_request(s: Settings, backend: str, row: dict) -> tuple[dict, dict]:
    """Run one request on a fresh store. Returns (the run's report, {member: [book ids on hold]})."""
    if backend == "stub":
        from main.model import StubModel

        model = StubModel(row.get("stub", "careful"))
    else:
        from main.model import gateway_model

        model = gateway_model(s)
    library = LibraryTools(MemoryStore())
    report = run_request(s, row["input"], row["member"], model, library, lambda approval: row["answer"] == "y",
                         request_id=f"eval-{row['id']}")
    members = {m for m, _ in library.store.rows} | {row["member"]}
    return report, {m: [h["book_id"] for h in library.get_holds(m)["holds"]] for m in sorted(members)}


@solver
def agent_run(s: Settings, backend: str):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        # the agent runs its own loop: in a thread, so Inspect's event loop stays free
        report, holds = await asyncio.to_thread(one_request, s, backend, state.metadata)
        state.metadata["report"], state.metadata["holds"] = report, holds
        state.output = ModelOutput.from_content(model=s.agent_model if backend == "gateway" else "stub",
                                                content=report.get("reply", ""))
        return state
    return solve


@scorer(metrics=[accuracy()])
def outcome(s: Settings):
    async def score(state: TaskState, target: Target) -> Score:
        report, expect = state.metadata["report"], state.metadata["expect"]
        problems = scoring.check(report, state.metadata["holds"], expect, s)
        return Score(value=0 if problems else 1, answer=report.get("reply", "")[:300],
                     explanation="; ".join(problems) or "as expected",
                     metadata={"tools_ran": report["tools_ran"], "tools_refused": report["tools_refused"]})
    return score


@scorer(metrics=[mean()])
def model_calls():
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=state.metadata["report"]["model_calls"])
    return score


@task
def member_requests(backend: str = "stub") -> Task:
    """backend: stub (offline) | gateway (the course LLM gateway; needs COURSE_GATEWAY_URL and KB_MCP_TOKEN)."""
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}")
    s = load_settings()
    if backend == "gateway":
        s.need("course_gateway_url")
        s.need("kb_mcp_token")
    return Task(dataset=MemoryDataset(samples(backend)), solver=agent_run(s, backend),
                scorer=[outcome(s), model_calls()], model="mockllm/model",
                metadata={"backend": backend, "agent_model": s.agent_model if backend == "gateway" else "stub"},
                name=f"member_requests_{backend}")
