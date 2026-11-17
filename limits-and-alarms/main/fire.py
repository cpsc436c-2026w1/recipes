"""Show each alarm firing in your sandbox, after main/run.py --target aws.

  uv run python main/fire.py --target aws                notification path: set-alarm-state ALARM, read the
                                                         alarm history for the SNS action, set it back to OK
  uv run python main/fire.py --target aws --synthetic    condition: put breaching datapoints into the app's
                                                         metrics and wait for CloudWatch itself to go to ALARM
  uv run python main/fire.py --target local              the notification path against moto

set-alarm-state proves the topic and the subscription work, not the threshold; the alarm goes back to its real
state at once, often within seconds (CloudWatch API Reference, SetAlarmState). --synthetic proves the threshold: it
sends the datapoints with PutMetricData, into the app's own metrics, so they show on the dashboard as app traffic.
"""

from __future__ import annotations

import argparse
import contextlib
import random
import time

import boto3

from alarms import alarm_specs
from config import ALARMS, Settings
from run import offline_aws


def notification_path(s: Settings, cw, wait_s: float = 10.0, read_history: bool = True) -> list[str]:
    lines = []
    for slug in ALARMS:
        name = s.alarm_name(slug)
        cw.set_alarm_state(AlarmName=name, StateValue="ALARM", StateReason="recipe fire test (set-alarm-state)")
        state = cw.describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]
        action = "not checked offline (moto has no alarm history)"
        if read_history:
            time.sleep(wait_s)
            hist = cw.describe_alarm_history(AlarmName=name, HistoryItemType="Action",
                                             MaxRecords=5)["AlarmHistoryItems"]
            sent = any("Successfully executed action" in h.get("HistorySummary", "") for h in hist)
            action = "sent" if sent else "not seen yet: wait a minute and run again"
        cw.set_alarm_state(AlarmName=name, StateValue="OK", StateReason="recipe fire test done")
        lines.append(f"{name:<32} state {state:<6} SNS action {action}")
    return lines


def synthetic_breach(s: Settings, cw, minutes: int = 3) -> list[str]:
    """Breaching datapoints for the errors and p95 alarms, one batch a minute, then wait for the real evaluation."""
    dims = [{"Name": "Service", "Value": s.service}]
    for m in range(minutes):
        cw.put_metric_data(Namespace=s.namespace, MetricData=[
            {"MetricName": "Errors", "Dimensions": dims, "Value": ALARMS["errors"]["threshold"], "Unit": "Count"},
            {"MetricName": "LatencyMs", "Dimensions": dims, "Unit": "Milliseconds",
             "Values": [ALARMS["p95-latency"]["threshold"] * 2 + random.random() for _ in range(20)]}])
        print(f"minute {m + 1}: breaching datapoints sent")
        time.sleep(60)
    names = [s.alarm_name("errors"), s.alarm_name("p95-latency")]
    for _ in range(10):
        states = {a["AlarmName"]: a["StateValue"] for a in cw.describe_alarms(AlarmNames=names)["MetricAlarms"]}
        if all(v == "ALARM" for v in states.values()):
            break
        time.sleep(30)
    return [f"{n:<32} state {states[n]}" for n in names] + [
        "the daily-cost alarm is checked offline (tests) or by a day of real spend; 0.50 CAD of synthetic cost "
        "would show up in your team's cost metric for 24 hours"]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--synthetic", action="store_true")
    args = p.parse_args(argv)
    s = Settings()
    with offline_aws() if args.target == "local" else contextlib.nullcontext():
        cw = boto3.client("cloudwatch", region_name=s.region)
        if args.target == "local":
            topic = boto3.client("sns", region_name=s.region).create_topic(Name=s.topic_name)["TopicArn"]
            for spec in alarm_specs(s, topic):
                cw.put_metric_alarm(**spec)
        if args.synthetic:
            out = synthetic_breach(s, cw)
        else:
            out = notification_path(s, cw, read_history=args.target == "aws")
        for line in out:
            print(line)


if __name__ == "__main__":
    main()
