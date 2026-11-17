"""Settings and limits, all in one place (README, "Decisions"). Nothing secret lives here.

Environment:
PREFIX           first part of every AWS resource name (default "recipe")
AWS_REGION       default ca-central-1
ALARM_EMAIL      optional: an address subscribed to the alarm topic (SNS sends a confirmation link first)
LAMBDA_ROLE_ARN  optional: an existing role Lambda can assume, with permission to write CloudWatch Logs. When it is
                 not set, --target aws creates {PREFIX}-app-role with CloudWatch Logs permissions only
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Limits outside the code path (README, "Decisions"). Every value is ours (assumed) for a course project:
# change them to your sandbox's budget and your app's traffic.
MONTHLY_BUDGET_USD = 25.0          # AWS Budgets, whole account; AWS bills and budgets in USD
BUDGET_ALERTS = [                  # (kind, percent of the budget)
    ("ACTUAL", 50.0), ("ACTUAL", 80.0), ("ACTUAL", 100.0), ("FORECASTED", 100.0)]
RESERVED_CONCURRENCY = 5           # at most 5 copies of the function run at once; the 6th request is throttled
LAMBDA_TIMEOUT_S = 30              # Lambda stops the function after 30 s, whatever the code does
LAMBDA_MEMORY_MB = 128

# Alarm thresholds (README, "Decisions", alarm rows). Ours (assumed), sized for a class demo on one course key.
ALARMS = {
    # 5 or more failed requests in 5 minutes: one failure is noise, five is an outage.
    "errors": {"metric": "Errors", "stat": "Sum", "period": 60 * 5, "periods": 1, "to_alarm": 1,
               "threshold": 5, "unit": "Count"},
    # p95 latency above 5 s in 2 of the last 3 minutes. M of N, because Lambda flushes logs on its own schedule
    # and a minute can arrive late or partly (CloudWatch User Guide, alarms on embedded metric format).
    "p95-latency": {"metric": "LatencyMs", "stat": "p95", "period": 60, "periods": 3, "to_alarm": 2,
                    "threshold": 5000, "unit": "Milliseconds"},
    # More than 0.50 CAD of model spend in one day (assumed threshold).
    "daily-cost": {"metric": "CostCAD", "stat": "Sum", "period": 86400, "periods": 1, "to_alarm": 1,
                   "threshold": 0.50, "unit": "None"},
    # Lambda's own metrics of the function, which need no line from the app: a request that Lambda stops at its
    # timeout, or a handler that fails to import, writes no EMF line and is counted only here (Serverless
    # Applications Lens, "Metrics and alerts": alarm on Lambda Errors and Throttles). One is worth a look.
    "lambda-errors": {"metric": "Errors", "stat": "Sum", "period": 60 * 5, "periods": 1, "to_alarm": 1,
                      "threshold": 1, "unit": "Count", "namespace": "AWS/Lambda"},
    "lambda-throttles": {"metric": "Throttles", "stat": "Sum", "period": 60 * 5, "periods": 1, "to_alarm": 1,
                         "threshold": 1, "unit": "Count", "namespace": "AWS/Lambda"},
}
# Why a task of the agent loop stopped (ext-runaway-loop/agent_loop.py); one line each on the dashboard.
STOP_REASONS = ["done", "step_cap", "deadline", "cost_cap", "budget_exhausted", "rate_limited"]

# Alarms at the threshold itself; the others only above it.
AT_THRESHOLD = {"errors", "lambda-errors", "lambda-throttles"}

# Canadian dollars per million tokens (input, output). Copied from the course LLM gateway's price table as
# recipes/dashboard-chat/main/model.py records it (2026-11-03); check it against the gateway's model list.
PRICES_CAD_PER_M = {"qwen3.6-35b-a3b": (0.05, 0.10), "qwen3.8-27b": (0.10, 0.20)}


def cost_cad(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES_CAD_PER_M[model]
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ca-central-1"))
    email: str = field(default_factory=lambda: os.environ.get("ALARM_EMAIL", ""))
    role_arn: str = field(default_factory=lambda: os.environ.get("LAMBDA_ROLE_ARN", ""))

    @property
    def service(self) -> str:
        return f"{self.prefix}-app"

    @property
    def role_name(self) -> str:
        return f"{self.prefix}-app-role"

    @property
    def function_name(self) -> str:
        return f"{self.prefix}-app"

    @property
    def namespace(self) -> str:
        return f"{self.prefix}/app"

    @property
    def topic_name(self) -> str:
        return f"{self.prefix}-limits-alarms"

    @property
    def budget_name(self) -> str:
        return f"{self.prefix}-monthly"

    @property
    def dashboard_name(self) -> str:
        return f"{self.prefix}-app"

    def alarm_name(self, slug: str) -> str:
        return f"{self.prefix}-app-{slug}"
