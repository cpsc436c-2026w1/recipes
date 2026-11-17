"""One CloudWatch dashboard: requests and errors, p50 and p95 latency, cost, Lambda throttles, alarm states."""

from __future__ import annotations

import json

from config import ALARMS, STOP_REASONS, Settings


def dashboard_body(s: Settings, alarm_arns: list[str]) -> str:
    dim = ["Service", s.service]
    ns = s.namespace

    def graph(title, metrics, x, y, stat="Sum", period=300):
        return {"type": "metric", "x": x, "y": y, "width": 12, "height": 6,
                "properties": {"title": title, "region": s.region, "view": "timeSeries", "stat": stat,
                               "period": period, "metrics": metrics}}

    widgets = [
        graph("Requests and errors (5 min)", [[ns, "Requests", *dim], [ns, "Errors", *dim]], 0, 0),
        graph("Latency p50 and p95 (ms, 1 min)",
              [[ns, "LatencyMs", *dim, {"stat": "p50"}], [ns, "LatencyMs", *dim, {"stat": "p95"}]], 12, 0,
              stat="p95", period=60),
        graph("Model spend (CAD per hour)", [[ns, "CostCAD", *dim]], 0, 6, period=3600),
        # Throttles above zero mean the reserved concurrency limit is doing its job.
        graph("Lambda throttles and concurrency",
              [["AWS/Lambda", "Throttles", "FunctionName", s.function_name],
               ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", s.function_name, {"stat": "Maximum"}]],
              12, 6, period=60),
        # Which limit stops the loop's tasks (ext-runaway-loop): one line per stop reason, tasks per hour. Empty
        # until the loop runs in your app (Agentic AI Lens AGENTCOST07-BP01: dashboards of cutoff activations).
        graph("Loop tasks by stop reason (per hour)",
              [[ns, "AgentSteps", "Service", s.service, "StopReason", r, {"stat": "SampleCount", "label": r}]
               for r in STOP_REASONS], 0, 12, period=3600),
        {"type": "alarm", "x": 0, "y": 18, "width": 24, "height": 3,
         "properties": {"title": "Alarms", "alarms": alarm_arns}},
    ]
    assert len(alarm_arns) == len(ALARMS)
    return json.dumps({"widgets": widgets})


def ensure_dashboard(s: Settings, cw, alarm_names: list[str]) -> str:
    arns = [a["AlarmArn"] for a in cw.describe_alarms(AlarmNames=alarm_names)["MetricAlarms"]]
    cw.put_dashboard(DashboardName=s.dashboard_name, DashboardBody=dashboard_body(s, arns))
    return (f"dashboard {s.dashboard_name}: https://{s.region}.console.aws.amazon.com/cloudwatch/home?"
            f"region={s.region}#dashboards/dashboard/{s.dashboard_name}")
