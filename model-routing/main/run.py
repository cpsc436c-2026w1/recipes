"""Run the routed app over a few requests and print one JSON line per request, then the total.

    uv run python -m main.run                          # stub model, offline
    uv run python -m main.run --model gateway          # course LLM gateway (COURSE_GATEWAY_URL, KB_MCP_TOKEN)
    uv run python -m main.run --request "Where is order 1043?"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from main.app import MisrouteLog, Models, RouteLog, handle
from main.config import OUTPUT_DIR, ROOT, Settings
from main.model import PRICES_CAD, GatewayModel, StubModel
from main.router import ROUTER_SYSTEM

HERE = Path(__file__).resolve().parent


def make_models(kind: str, settings: Settings, stub_file: Path = HERE / "stub_replies.json") -> Models:
    # A model without a price would be counted at 0 CAD: the totals, and the eval's comparison, would be wrong
    # without a sign of it. Checked before any call.
    for name in (settings.small_model, settings.large_model):
        if name not in PRICES_CAD:
            raise SystemExit(f"no price for model {name!r}: add it to PRICES_CAD in main/model.py "
                             f"(known: {', '.join(sorted(PRICES_CAD))})")
    if kind == "gateway":
        client = GatewayModel(settings.gateway_url, settings.token)
    else:
        client = StubModel.from_file(stub_file, ROUTER_SYSTEM)
    return Models(client, small=settings.small_model, large=settings.large_model)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", choices=["stub", "gateway"], default="stub")
    p.add_argument("--request", action="append", help="a request to route; repeat for several")
    p.add_argument("--log", default=str(OUTPUT_DIR / "misroutes.jsonl"))
    p.add_argument("--routes", default=str(OUTPUT_DIR / "routes.jsonl"),
                   help="one JSON line per request, without its text: label, path, cost")
    a = p.parse_args(argv)
    models = make_models(a.model, Settings())
    log = MisrouteLog(Path(a.log))
    routes = RouteLog(Path(a.routes))
    texts = a.request or [t for t in (HERE / "requests.txt").read_text().splitlines() if t.strip()]
    total, large, failed = 0.0, 0, 0
    for t in texts:
        out = handle(t, models, log, routes=routes)
        total += out["cost_cad"]
        large += out["large_model_calls"]
        failed += bool(out["error"])
        row = {k: out[k] for k in ("request", "label", "path", "large_model_calls", "reply")}
        if out["error"]:
            row["error"] = out["error"]
        print(json.dumps({**row, "cost_cad": f"{out['cost_cad']:.8f}"}))
        if out["error"] and any(f"HTTP {code}" in out["error"] for code in (401, 403)):
            # The key, or the model for this key, is wrong: every later request would be refused the same way.
            print(f"stopped after {texts.index(t) + 1} of {len(texts)} requests: the gateway refused the key or "
                  f"the model (HTTP 401 or 403); the remaining requests were not sent")
            texts = texts[: texts.index(t) + 1]
            break
    note = " (stub: tokens estimated from characters)" if a.model == "stub" else ""
    shown = Path(a.log).resolve()
    shown = shown.relative_to(ROOT) if shown.is_relative_to(ROOT) else shown
    print(f"model: {a.model}; {len(texts)} requests; large-model calls {large}; total {total:.6f} CAD{note}; "
          f"{len(log.lines)} misroutes logged in {shown}")
    if failed:
        # every request was tried; the exit code says that the run is not a clean one
        raise SystemExit(f"{failed} of {len(texts)} requests ended in error: read the `error` field of their lines")


if __name__ == "__main__":
    main()
