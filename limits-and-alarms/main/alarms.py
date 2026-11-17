"""Five CloudWatch alarms, three on the app's EMF metrics and two on Lambda's own metrics of the function, and the
SNS topic they notify.

  {PREFIX}-app-errors       Sum of Errors over 5 minutes >= 5
  {PREFIX}-app-p95-latency  p95 of LatencyMs per minute > 5000 ms in 2 of the last 3 minutes
  {PREFIX}-app-daily-cost   Sum of CostCAD over 1 day > 0.50
  {PREFIX}-app-lambda-errors     Sum of the function's AWS/Lambda Errors over 5 minutes >= 1 (timeouts, crashes)
  {PREFIX}-app-lambda-throttles  Sum of the function's AWS/Lambda Throttles over 5 minutes >= 1

Thresholds live in config.ALARMS (ours, assumed). The same topic receives the AWS Budgets alerts (main/limits.py).
"""

from __future__ import annotations

import json

from config import ALARMS, AT_THRESHOLD, Settings
from limits import RECIPE_TAG, not_ours


def alarm_specs(s: Settings, topic_arn: str) -> list[dict]:
    """The PutMetricAlarm arguments for each alarm. Tests read these same dicts (main/alarm_eval.py)."""
    specs = []
    for slug, a in ALARMS.items():
        stat = {"ExtendedStatistic": a["stat"]} if a["stat"].startswith("p") else {"Statistic": a["stat"]}
        specs.append({
            "AlarmName": s.alarm_name(slug),
            "AlarmDescription": f"limits-and-alarms recipe: {slug} (README, Decisions)",
            # The app's own metrics by service; Lambda's metrics of the function by its name.
            "Namespace": a.get("namespace", s.namespace),
            "MetricName": a["metric"],
            "Dimensions": ([{"Name": "FunctionName", "Value": s.function_name}] if a.get("namespace") == "AWS/Lambda"
                           else [{"Name": "Service", "Value": s.service}]),
            "Period": a["period"],
            "EvaluationPeriods": a["periods"],
            "DatapointsToAlarm": a["to_alarm"],
            "Threshold": a["threshold"],
            # Errors alarm at the threshold itself; latency and cost only above it.
            "ComparisonOperator": ("GreaterThanOrEqualToThreshold" if slug in AT_THRESHOLD
                                   else "GreaterThanThreshold"),
            # No traffic is not an outage (README, "Decisions": missing data).
            "TreatMissingData": "notBreaching",
            "AlarmActions": [topic_arn],
            "OKActions": [topic_arn],
            **stat,
        })
    return specs


def topic_policy(topic_arn: str, account_id: str) -> str:
    """Let CloudWatch alarms and AWS Budgets of this account publish to the topic."""
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "budgets", "Effect": "Allow", "Principal": {"Service": "budgets.amazonaws.com"},
         "Action": "SNS:Publish", "Resource": topic_arn,
         "Condition": {"StringEquals": {"aws:SourceAccount": account_id}}},
        {"Sid": "cloudwatch", "Effect": "Allow", "Principal": {"Service": "cloudwatch.amazonaws.com"},
         "Action": "SNS:Publish", "Resource": topic_arn,
         "Condition": {"StringEquals": {"aws:SourceAccount": account_id}}},
    ]})


def ensure_topic(s: Settings, sns, account_id: str) -> str:
    # A topic with this name that does not carry the recipe's tag is someone else's: its access policy is not
    # replaced and nothing is subscribed to it (README, "Decisions": what the recipe may change or delete).
    for page in sns.get_paginator("list_topics").paginate():
        for t in page["Topics"]:
            if t["TopicArn"].split(":")[-1] == s.topic_name and \
                    RECIPE_TAG not in sns.list_tags_for_resource(ResourceArn=t["TopicArn"])["Tags"]:
                raise SystemExit(not_ours("sns topic", s.topic_name))
    arn = sns.create_topic(Name=s.topic_name, Tags=[RECIPE_TAG])["TopicArn"]
    sns.set_topic_attributes(TopicArn=arn, AttributeName="Policy", AttributeValue=topic_policy(arn, account_id))
    if s.email:
        sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=s.email)
    return arn


def ensure_alarms(s: Settings, cw, topic_arn: str) -> list[str]:
    names = []
    for spec in alarm_specs(s, topic_arn):
        cw.put_metric_alarm(**spec)  # creates or replaces: running the script twice changes nothing
        names.append(spec["AlarmName"])
    return names
