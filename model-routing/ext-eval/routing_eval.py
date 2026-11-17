"""Routing eval: label accuracy on hand-labelled requests, and cost per request before and after routing.

    uv run python ext-eval/routing_eval.py                                  # stub router labels, offline
    uv run python ext-eval/routing_eval.py --model gateway                  # the course LLM gateway: 24 router calls + answers
    uv run python ext-eval/routing_eval.py --min-accuracy 0.9               # exit 1 below 0.9: use it as a check in CI

Two policies over the same requests:
- all-large: before routing. Every request goes to the large model with the hard path's prompt.
- routed: main/app.py. The small model labels; app code runs the path for the label.

The eval measures the router and the cost. It does not judge whether each reply is right: that needs an answer
eval per path (ext-eval/README.md, "What it does not measure").
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main.app import MODEL_CALL_FAILED, MisrouteLog, handle  # noqa: E402
from main.config import OUTPUT_DIR, Settings        # noqa: E402
from main.paths import HARD_MAX_TOKENS, HARD_SYSTEM  # noqa: E402
from main.router import LABELS                       # noqa: E402
from main.run import make_models                     # noqa: E402

HERE = Path(__file__).resolve().parent


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_eval(requests: list[dict], models, log: MisrouteLog) -> dict:
    routed = []
    for req in requests:
        out = handle(req["text"], models, log, request_id=req["id"])
        if out["error"]:
            # A request that ended in error has no label to compare: counting it as a wrong label would report the
            # gateway's refusal as the router's mistake. The eval stops; no report is written.
            raise SystemExit(f"eval stopped at {req['id']}: {out['error']}")
        out["gold"] = req["gold"]
        routed.append(out)
        if out["label"] != req["gold"]:
            log.write(request_id=req["id"], request=req["text"], label=out["label"], gold=req["gold"],
                      reason="label differs from the hand label", sent_to=out["path"])

    baseline = []
    for req in requests:
        try:
            r = models.chat(models.large, HARD_SYSTEM, f"Question: {req['text']}", HARD_MAX_TOKENS)
        except MODEL_CALL_FAILED as e:
            raise SystemExit(f"eval stopped at {req['id']} (all-large): {type(e).__name__}: {e}"[:300]) from None
        baseline.append(r.cost_cad)

    n = len(requests)
    correct = sum(o["label"] == o["gold"] for o in routed)
    per_label = {}
    for label in LABELS:
        rows = [o for o in routed if o["gold"] == label]
        per_label[label] = {"n": len(rows), "correct": sum(o["label"] == label for o in rows),
                            "got": dict(Counter(o["label"] for o in rows))}
    # Out-of-scope by the hand label, whatever the router said: a request the router sent on to `hard` counts here.
    oos = [o for o in routed if o["gold"] == "out_of_scope"]
    routed_cost = sum(o["cost_cad"] for o in routed)
    per_path = {}
    for path in LABELS:                          # the path that ran, which is not always the label
        rows = [o for o in routed if o["path"] == path]
        cost = sum(o["cost_cad"] for o in rows)
        per_path[path] = {"n": len(rows), "cost_cad": cost, "share": cost / routed_cost if routed_cost else 0.0,
                          "large_calls": sum(o["large_model_calls"] for o in rows)}
    return {
        "n": n,
        "per_path": per_path,
        "accuracy": correct / n if n else 0.0,
        "per_label": per_label,
        "policies": {
            "all-large": {"cost_cad": sum(baseline), "per_request": sum(baseline) / n, "large_calls": n},
            "routed": {"cost_cad": sum(o["cost_cad"] for o in routed),
                       "per_request": sum(o["cost_cad"] for o in routed) / n,
                       "large_calls": sum(o["large_model_calls"] for o in routed),
                       "router_cost_cad": sum(c["cost_cad"] for o in routed for c in o["calls"]
                                              if c["part"] == "router")},
        },
        "out_of_scope_large_calls": sum(o["large_model_calls"] for o in oos),
        "misroutes": [o for o in routed if o["label"] != o["gold"]],
        "rows": routed,
    }


def report(res: dict, model_kind: str) -> str:
    lines = [f"Routing eval, {res['n']} labelled requests, model: {model_kind}"
             + (" (stub: labels written by hand, tokens estimated from characters)" if model_kind == "stub" else ""),
             "", f"Label accuracy: {res['accuracy']:.2f}", "",
             "| hand label | requests | labelled right | router labels |", "|---|---:|---:|---|"]
    for label, s in res["per_label"].items():
        got = ", ".join(f"{k} {v}" for k, v in sorted(s["got"].items()))
        lines.append(f"| {label} | {s['n']} | {s['correct']} | {got} |")
    a, r = res["policies"]["all-large"], res["policies"]["routed"]
    lines += ["", "| policy | cost per request (CAD) | total (CAD) | large-model calls | of which router (CAD) |",
              "|---|---:|---:|---:|---:|",
              f"| all-large (before routing) | {a['per_request']:.8f} | {a['cost_cad']:.6f} | {a['large_calls']} | 0 |",
              f"| routed | {r['per_request']:.8f} | {r['cost_cad']:.6f} | {r['large_calls']} | "
              f"{r['router_cost_cad']:.6f} |",
              "", f"Routed cost is {r['cost_cad'] / a['cost_cad']:.2f} of all-large." if a["cost_cad"] else "",
              "", "Routed, by the path that ran (router call included):", "",
              "| path | requests | cost (CAD) | share of routed cost | large-model calls |", "|---|---:|---:|---:|---:|",
              *[f"| {path} | {s['n']} | {s['cost_cad']:.6f} | {s['share']:.2f} | {s['large_calls']} |"
                for path, s in res["per_path"].items()],
              f"Large-model calls on requests hand-labelled out_of_scope: {res['out_of_scope_large_calls']}", "",
              "Labelled differently from the hand label:"]
    for o in res["misroutes"]:
        lines.append(f"- {o['request_id']} hand {o['gold']}, router {o['label']}, ran {o['path']}: {o['request']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", choices=["stub", "gateway"], default="stub")
    p.add_argument("--requests", default=str(HERE / "labelled.jsonl"))
    p.add_argument("--min-accuracy", type=float, default=None)
    p.add_argument("--out", default=None, help="write the report here as well (default output/eval-<model>.md)")
    p.add_argument("--log", default=str(OUTPUT_DIR / "eval-misroutes.jsonl"),
                   help="where the mislabelled requests go, one JSON line each")
    a = p.parse_args(argv)
    models = make_models(a.model, Settings(), stub_file=HERE / "stub_replies.json")
    log = MisrouteLog(Path(a.log))
    res = run_eval(load(Path(a.requests)), models, log)
    text = report(res, a.model)
    print(text, end="")
    Path(a.out or OUTPUT_DIR / f"eval-{a.model}.md").write_text(text)
    if a.min_accuracy is not None and res["accuracy"] < a.min_accuracy:
        print(f"FAIL: accuracy {res['accuracy']:.2f} below {a.min_accuracy:.2f}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
