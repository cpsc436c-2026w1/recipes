"""The team's app as a Lambda function, instrumented: errors, latency and cost per request as EMF log lines.

The work is a stand-in for your handler: it "calls the model" by sleeping and counting tokens from the event,
so the recipe runs without a model. Event fields (all optional), used by main/run.py to make traffic:
  {"tokens_in": 800, "tokens_out": 120, "delay_ms": 50, "fail": false, "model": "qwen3.6-35b-a3b"}

Deployed as a zip with app.py, emf.py and config.py at its root (main/limits.py); handler "app.handler".
"""

from __future__ import annotations

import os
import time

from config import cost_cad
from emf import emit, request_line

NAMESPACE = os.environ.get("METRIC_NAMESPACE", "recipe/app")
SERVICE = os.environ.get("SERVICE_NAME", "recipe-app")


def do_work(event: dict) -> dict:
    """Your handler's real work goes here: retrieval, the model call through the course LLM gateway, tools."""
    time.sleep(event.get("delay_ms", 20) / 1000)
    if event.get("fail"):
        raise RuntimeError("upstream model call failed")
    return {"reply": "ok", "tokens_in": event.get("tokens_in", 800), "tokens_out": event.get("tokens_out", 120)}


def handler(event, context, out=None, clock=time.monotonic):
    """Every request leaves exactly one EMF line, in `finally`, so a failed request is counted too."""
    t0 = clock()
    model = event.get("model", "qwen3.6-35b-a3b")
    error, error_type, result = False, "", {}
    try:
        # A model that is not in the price table is refused before the work: its spend could not be counted,
        # and the daily-cost alarm would not see it (README, "Decisions": estimated model cost per request).
        cost_cad(model, 0, 0)
        result = do_work(event)
        return {"statusCode": 200, "body": result["reply"]}
    except Exception as e:  # noqa: BLE001  README, "Decisions": errors are counted, then the caller gets a 500
        error, error_type = True, type(e).__name__
        return {"statusCode": 500, "body": "internal error"}
    finally:
        tin, tout = result.get("tokens_in", 0), result.get("tokens_out", 0)
        # No tokens, no price look-up: the line is written whatever went wrong above.
        cost = cost_cad(model, tin, tout) if tin or tout else 0.0
        emit(request_line(NAMESPACE, SERVICE, latency_ms=(clock() - t0) * 1000, error=error,
                          cost_cad=cost, tokens_in=tin, tokens_out=tout,
                          request_id=getattr(context, "aws_request_id", ""), route=event.get("route", "/ask"),
                          extra={"errorType": error_type} if error else None),  # a plain field, not a metric
             out=out)
