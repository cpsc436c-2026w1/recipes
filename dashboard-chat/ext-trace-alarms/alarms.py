"""Metrics per request and three CloudWatch alarms: errors, slow answers, daily cost.

Metrics go to namespace {PREFIX}/dashboard-chat with PutMetricData, one call per request: Requests, Errors,
LatencyMs, CostCAD. The alarms watch them and notify the SNS topic {PREFIX}-chat-alarms (subscribe your email with
--email; SNS sends a confirmation link first).

Thresholds are ours (assumed), sized for a class demo on one course LLM gateway key; change them in THRESHOLDS.
"""

from __future__ import annotations

import boto3

THRESHOLDS = {
    # errors: 3 or more failed questions in 5 minutes. One failure is noise; three within five minutes is an outage.
    "errors": {"metric": "Errors", "stat": "Sum", "period": 300, "periods": 1, "threshold": 3},
    # slow: the 90th percentile answer time at 15 s or more in 3 periods of 5 minutes running.
    "slow-answers": {"metric": "LatencyMs", "stat": "p90", "period": 300, "periods": 3, "threshold": 15000},
    # cost: 0.25 CAD or more in the last 24 hours, assumed threshold.
    "daily-cost": {"metric": "CostCAD", "stat": "Sum", "period": 86400, "periods": 1, "threshold": 0.25},
}


def put_request_metrics(ans: dict, namespace: str, region: str, client=None) -> None:
    cw = client or boto3.client("cloudwatch", region_name=region)
    try:
        cw.put_metric_data(Namespace=namespace, MetricData=[
            {"MetricName": "Requests", "Value": 1, "Unit": "Count"},
            {"MetricName": "Errors", "Value": 1 if ans["status"] == "error" else 0, "Unit": "Count"},
            {"MetricName": "LatencyMs", "Value": ans["latency_ms"], "Unit": "Milliseconds"},
            {"MetricName": "CostCAD", "Value": ans["cost_cad"], "Unit": "None"},
        ])
    except Exception as e:  # metrics never break the answer
        print(f"put_metric_data failed: {e}")


def ensure_alarms(prefix: str, namespace: str, topic_name: str, region: str, email: str | None = None,
                  cw=None, sns=None) -> list[str]:
    cw = cw or boto3.client("cloudwatch", region_name=region)
    sns = sns or boto3.client("sns", region_name=region)
    topic_arn = sns.create_topic(Name=topic_name)["TopicArn"]  # returns the existing topic if it is there
    if email:
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
    names = []
    for slug, t in THRESHOLDS.items():
        name = f"{prefix}{slug}"
        kw = {"ExtendedStatistic": t["stat"]} if t["stat"].startswith("p") else {"Statistic": t["stat"]}
        cw.put_metric_alarm(
            AlarmName=name, Namespace=namespace, MetricName=t["metric"], Period=t["period"],
            EvaluationPeriods=t["periods"], Threshold=t["threshold"], ComparisonOperator="GreaterThanOrEqualToThreshold",
            TreatMissingData="notBreaching",  # no traffic is not an alarm
            AlarmActions=[topic_arn], AlarmDescription=f"dashboard-chat recipe: {slug}", **kw)
        names.append(name)
    return names
