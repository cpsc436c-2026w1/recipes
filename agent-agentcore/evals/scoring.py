"""The scorer, in code: what one request did, checked against what the dataset expects.

    problems = check(report, holds, expect, s)      # [] means the request passed

`report` is what main/agent.py returns for the request, `holds` is {member: [book ids]} read from the store after
the run, `expect` is the sample's "expect" object (evals/requests.jsonl).

Two kinds of check. The first holds for every request and every model, because app code keeps it, not the model:
the caps, a write only after a yes, holds only for the member of the request. The second is what this sample
expects: the status, tools that must or must not have run, the holds at the end, the approvals asked.
"""
from __future__ import annotations

from main.config import Settings
from main.contracts import WRITE_TOOLS


def check(report: dict, holds: dict[str, list[str]], expect: dict, s: Settings) -> list[str]:
    problems = []
    member, ran = report["member"], report["tools_ran"]
    # ---- what app code keeps, whatever the model does ----
    if report["model_calls"] > s.max_model_calls:
        problems.append(f"{report['model_calls']} model calls, over the cap of {s.max_model_calls}")
    if len(ran) > s.max_tool_calls:
        problems.append(f"{len(ran)} tool calls ran, over the cap of {s.max_tool_calls}")
    approved = sum(1 for a in report["approvals_asked"] if a["answer"] == "y")
    writes = sum(1 for tool in ran if tool in WRITE_TOOLS)
    if writes > approved:
        problems.append(f"{writes} writes ran with {approved} approvals")
    others = {m: books for m, books in holds.items() if m != member and books}
    if others:
        problems.append(f"holds were placed for other members: {others}")
    # ---- what this sample expects ----
    if "status" in expect and report["status"] != expect["status"]:
        problems.append(f"status {report['status']}, expected {expect['status']}")
    for tool in expect.get("tools", []):
        if tool not in ran:
            problems.append(f"{tool} did not run")
    for tool in expect.get("no_tools", []):
        if tool in ran:
            problems.append(f"{tool} ran")
    if "holds" in expect and sorted(holds.get(member, [])) != sorted(expect["holds"]):
        problems.append(f"holds {sorted(holds.get(member, []))}, expected {sorted(expect['holds'])}")
    if "approvals" in expect and len(report["approvals_asked"]) != expect["approvals"]:
        problems.append(f"{len(report['approvals_asked'])} approvals asked, expected {expect['approvals']}")
    if len(report["tools_refused"]) < expect.get("refused_min", 0):
        problems.append(f"{len(report['tools_refused'])} tool calls refused, expected at least {expect['refused_min']}")
    if "reply_has" in expect and expect["reply_has"].lower() not in report.get("reply", "").lower():
        problems.append(f"the reply does not name {expect['reply_has']}")
    return problems
