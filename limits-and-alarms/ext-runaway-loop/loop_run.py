"""Five tasks through the agent loop, offline: one that finishes, four runaways stopped by different limits.

  uv run python ext-runaway-loop/loop_run.py

The model is a stub behind a stand-in for the course LLM gateway (fake_gateway.py); the clock is simulated, so
the run takes under a second and spends nothing. EMF lines go to output/loop-emf.jsonl.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "main"), str(ROOT / "ext-runaway-loop")]

from agent_loop import Limits, run_agent  # noqa: E402
from fake_gateway import FakeGateway, finishes_after, runaway  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def tools():
    return {"search": lambda q: f"no result for {q}"}


def scenarios():
    """(label, gateway behaviour, gateway budget CAD, loop limits). Budgets are small so the output stays short."""
    return [
        ("finishes in 3 steps", finishes_after(2), 1.0, Limits()),
        ("runaway, step cap", runaway, 1.0, Limits()),
        ("runaway, slow model, deadline", runaway, 1.0, Limits(max_steps=100)),
        ("runaway, cost cap", runaway, 1.0, Limits(max_steps=None, deadline_s=None)),
        ("runaway, loop limits all off", runaway, 0.05,
         Limits(max_steps=None, deadline_s=None, max_cost_cad=None)),
    ]


def main(argv: list[str] | None = None, out=None) -> list[str]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--emf", type=Path, default=ROOT / "output" / "loop-emf.jsonl",
                   help="where the EMF lines go (default output/loop-emf.jsonl)")
    args = p.parse_args(argv if argv is not None else [])
    out = out or sys.stdout
    emf = io.StringIO()
    lines = [f"{'task':<34} {'stopped by':<17} {'steps':>6} {'spent CAD':>10} {'seconds':>8}"]
    for label, behaviour, budget, limits in scenarios():
        clock = Clock()
        gw = FakeGateway(behaviour, budget_cad=budget, clock=clock, call_s=4.0 if "slow" in label else 1.0)
        r = run_agent(label, call_model=gw, tools=tools(), limits=limits, clock=clock, out=emf)
        lines.append(f"{label:<34} {r.stop_reason:<17} {r.steps:>6} {r.cost_cad:>10.5f} {r.seconds:>8.0f}")
    args.emf.parent.mkdir(parents=True, exist_ok=True)
    args.emf.write_text(emf.getvalue())
    for line in lines:
        print(line, file=out)
    return lines


if __name__ == "__main__":
    main(sys.argv[1:])
