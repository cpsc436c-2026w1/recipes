"""Set up the instrument set and show each alarm's condition firing on synthetic traffic.

  uv run python main/run.py --target local    moto stands in for AWS; alarms checked by main/alarm_eval.py
  uv run python main/run.py --target aws      your sandbox: budget, function, limits, alarms, dashboard,
                                              then a few real requests (creates {PREFIX}-app-role
                                              unless LAMBDA_ROLE_ARN names an existing role)

After a real run: uv run python main/fire.py --target aws   (each alarm's notification path)
Clean up:          uv run python clean_up.py
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time

import boto3

from alarm_eval import evaluate
from alarms import alarm_specs, ensure_alarms, ensure_topic
from config import Settings
from dashboard import ensure_dashboard
from emf import request_line
from limits import ensure_budget, ensure_concurrency, ensure_function, ensure_role

MIN = 60_000  # one minute in ms


def offline_aws():
    from moto import mock_aws

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ[k] = "testing"
    os.environ.pop("AWS_PROFILE", None)
    return mock_aws()


def clients(s: Settings) -> dict:
    return {"cw": boto3.client("cloudwatch", region_name=s.region), "sns": boto3.client("sns", region_name=s.region),
            "lam": boto3.client("lambda", region_name=s.region), "iam": boto3.client("iam", region_name=s.region),
            # AWS Budgets has one endpoint for all Regions, in us-east-1; the budget covers the whole account.
            "budgets": boto3.client("budgets", region_name="us-east-1"),
            "sts": boto3.client("sts", region_name=s.region)}


# What GetAccountSettings returns in an account with the default quota (Lambda Developer Guide, concurrency quotas).
DEFAULT_ACCOUNT = {"AccountLimit": {"ConcurrentExecutions": 1000, "UnreservedConcurrentExecutions": 1000}}


def set_up(s: Settings, c: dict, role_arn: str = "", account_settings: dict | None = None,
           wait_s: float = 3.0) -> tuple[list[str], str, list[str]]:
    """`role_arn` empty: create or reuse {PREFIX}-app-role (README, "Decisions", IAM)."""
    account = c["sts"].get_caller_identity()["Account"]
    lines = []
    if role_arn:
        lines.append(f"role from LAMBDA_ROLE_ARN: {role_arn.split('/')[-1]}")
    else:
        role_arn, line = ensure_role(s, c["iam"], account)
        lines.append(line)
    topic = ensure_topic(s, c["sns"], account)
    lines.append(f"topic {s.topic_name}" + (f" (confirm the email sent to {s.email})" if s.email else ""))
    lines.append(ensure_budget(s, c["budgets"], account, topic))
    lines.append(ensure_function(s, c["lam"], role_arn, wait_s))
    lines.append(ensure_concurrency(s, c["lam"], account_settings))
    names = ensure_alarms(s, c["cw"], topic)
    lines.append(f"alarms {', '.join(names)}")
    lines.append(ensure_dashboard(s, c["cw"], names))
    return lines, topic, names


# --- synthetic traffic: EMF lines as the app writes them, with chosen times and values --------------------------

def synthetic(s: Settings, now_ms: int, kind: str) -> list[dict]:
    """Ten minutes of traffic, 10 requests a minute, ending at now_ms. `kind` picks what goes wrong."""
    docs = []
    for minute in range(10, 0, -1):
        for i in range(10):
            t = now_ms - minute * MIN + i * 5000 + 1
            late = minute <= 3
            latency = 6500 if kind == "slow" and late and i < 2 else 400 + 30 * i  # 2 of 10 slow -> p95 slow
            error = kind == "errors" and late and i < 2                           # 6 errors in the last 3 min
            cost = 0.01 if kind == "expensive" else 0.00006                        # 100 requests x 0.01 = 1 CAD
            docs.append(request_line(s.namespace, s.service, latency_ms=latency, error=error, cost_cad=cost,
                                     tokens_in=800, tokens_out=120, request_id=f"r{minute}-{i}", ts_ms=t))
    return docs


def check_offline(s: Settings, topic: str) -> list[str]:
    now = int(time.time() * 1000)
    # Synthetic EMF lines feed only the app's own metrics; Lambda's metrics of the function come from Lambda.
    specs = [sp for sp in alarm_specs(s, topic) if sp["Namespace"] == s.namespace]
    lines = [f"{'traffic':<10} " + "  ".join(f"{sp['AlarmName'].split('-app-')[1]:<12}" for sp in specs)]
    for kind in ("normal", "errors", "slow", "expensive"):
        docs = synthetic(s, now, kind)
        states = [evaluate(sp, docs, now)[0] for sp in specs]
        lines.append(f"{kind:<10} " + "  ".join(f"{st:<12}" for st in states))
    return lines


def send_requests(s: Settings, lam, n: int = 12) -> str:
    """A few real requests, every sixth one failing, so the dashboard shows data within minutes.

    The line it returns says what came back: it is printed only when every request answered as planned (200, or
    500 for the ones sent to fail). Otherwise the run stops with what went wrong."""
    failing, broken, wrong, codes = 0, [], [], {}
    for i in range(n):
        fail = i % 6 == 5
        failing += fail
        r = lam.invoke(FunctionName=s.function_name, Payload=json.dumps({"fail": fail, "delay_ms": 50}).encode())
        text = r["Payload"].read().decode(errors="replace")
        # The handler answers 500 for a failed request and still writes its metric line. FunctionError means the
        # handler itself broke (import error, timeout, an exception outside its try): no line, no metric.
        if r.get("FunctionError"):
            broken.append(text[:300])
            continue
        try:
            code = json.loads(text).get("statusCode")
        except (ValueError, AttributeError):
            code = None
        codes[code] = codes.get(code, 0) + 1
        if code != (500 if fail else 200):
            wrong.append(f"request {i + 1} (sent to {'fail' if fail else 'succeed'}) answered {code}: {text[:200]}")
    if broken:
        raise SystemExit(f"{len(broken)} of {n} requests broke the function {s.function_name} and wrote no metric "
                         f"line: {broken[0]}\nFix main/app.py and run again, or clean up: uv run python clean_up.py --yes")
    if wrong:
        seen = ", ".join(f"{v} answered {k}" for k, v in sorted(codes.items(), key=lambda kv: str(kv[0])))
        raise SystemExit(f"sent {n} requests, {failing} of them meant to fail; {seen}. {len(wrong)} did not answer "
                         f"as planned, first: {wrong[0]}\nThe function's log group says why "
                         f"(/aws/lambda/{s.function_name}, errorType). Fix main/app.py and run again.")
    return f"sent {n} requests ({failing} failing); metrics reach the dashboard in a few minutes"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    s = Settings()
    with offline_aws() if args.target == "local" else contextlib.nullcontext():
        c = clients(s)
        print(f"target {args.target}  prefix {s.prefix}  region {s.region}")
        lines, topic, names = set_up(s, c, s.role_arn, DEFAULT_ACCOUNT if args.target == "local" else None)
        for line in lines:
            print(line)
        if args.target == "local":
            print("alarm states on ten minutes of synthetic traffic (main/alarm_eval.py):")
            for line in check_offline(s, topic):
                print(("  " + line).rstrip())
        else:
            print(send_requests(s, c["lam"]))
            print("next: uv run python main/fire.py --target aws    then: uv run python clean_up.py --yes")


if __name__ == "__main__":
    main()
