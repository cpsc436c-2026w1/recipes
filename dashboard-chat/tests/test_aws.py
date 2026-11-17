"""The AWS paths against moto: tables, trace items, cache items, spans, metrics, alarms, clean-up."""

import boto3
from fakes import StillDeleting
from moto import mock_aws

from main.chat import answer
from main.config import Settings
from main.infra import ensure_table
from main.traces import DynamoTraces


@mock_aws
def test_trace_item_in_dynamodb(engine, stub):
    s = Settings()
    ensure_table(s.traces_table, s.region, key="request_id")
    ddb = boto3.client("dynamodb", region_name=s.region)
    ttl = ddb.describe_time_to_live(TableName=s.traces_table)["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "expires_at"
    traces = DynamoTraces(s.traces_table, s.region)
    a = answer("How many orders are there?", user_id="c-0007", model=stub, engine=engine, traces=traces)
    item = traces.get(a.request_id)
    assert item["status"] == "answered" and item["sql"].startswith("SELECT")
    assert "user_id" not in item and len(item["steps"]) == 3


@mock_aws
def test_cache_in_dynamodb(engine, stub, version):
    from cache import DynamoCache, cached_answer

    s = Settings()
    ensure_table(s.cache_table, s.region, key="cache_key")
    cache = DynamoCache(s.cache_table, s.region)

    def ask(q):
        return answer(q, user_id="c-0007", model=stub, engine=engine, data_version=version)

    first, hit1 = cached_answer("How many orders are there?", user_id="c-0007", cache=cache,
                                data_version=version, answer_version='test', ask=ask)
    second, hit2 = cached_answer("how many orders are there", user_id="c-0012", cache=cache,
                                 data_version=version, answer_version='test', ask=ask)
    assert (hit1, hit2) == (False, True) and second["rows"] == first["rows"]


@mock_aws
def test_spans_alarms_and_clean_up(engine, stub):
    from alarms import THRESHOLDS, ensure_alarms, put_request_metrics
    from spans import DynamoSpans, spans_of

    from clean_up import clean_up

    s = Settings()
    for name, key, sk in ((s.traces_table, "request_id", None), (s.cache_table, "cache_key", None),
                          (s.spans_table, "trace_id", "span_id")):
        ensure_table(name, s.region, key=key, sort_key=sk)
    sink = DynamoSpans(s.spans_table, s.region)
    a = answer("What were total sales in Vancouver?", user_id="c-0007", model=stub, engine=engine)
    sink.put_all(spans_of(a))
    trace = sink.trace(a.request_id)
    assert [i["span_id"] for i in trace] == ["0000", "0001", "0002", "0003"]
    assert all(i["parent_id"] == "0000" for i in trace[1:])
    put_request_metrics(a.to_dict(), s.metric_namespace, s.region)
    cw = boto3.client("cloudwatch", region_name=s.region)
    names = {m["MetricName"] for m in cw.list_metrics(Namespace=s.metric_namespace)["Metrics"]}
    assert names == {"Requests", "Errors", "LatencyMs", "CostCAD"}
    created = ensure_alarms(s.alarm_prefix, s.metric_namespace, s.topic_name, s.region)
    assert len(created) == len(THRESHOLDS)
    alarms = {a["AlarmName"]: a for a in cw.describe_alarms(AlarmNamePrefix=s.alarm_prefix)["MetricAlarms"]}
    assert alarms["test-chat-daily-cost"]["Period"] == 86400
    assert alarms["test-chat-slow-answers"]["ExtendedStatistic"] == "p90"

    done = clean_up(s)
    assert len(done.removed) == 3 + 3 + 1 and not done.failed
    assert boto3.client("dynamodb", region_name=s.region).list_tables()["TableNames"] == []
    assert cw.describe_alarms(AlarmNamePrefix=s.alarm_prefix)["MetricAlarms"] == []
    again = clean_up(s)  # a second run finds nothing left, and that is not a failure
    assert again.removed == [] and not again.failed and len(again.absent) == 3 + 3 + 1  # each alarm by name


def test_clean_up_dry_run_makes_no_aws_call(monkeypatch):
    from alarms import THRESHOLDS

    import clean_up as cu

    def refuse(*a, **k):
        raise AssertionError("dry run created an AWS client")

    monkeypatch.setattr(cu.boto3, "client", refuse)
    s = Settings()
    names = cu.clean_up(s, dry_run=True).removed
    assert set(cu.ALARM_SLUGS) == set(THRESHOLDS)
    assert len(names) == 3 + 3 + 1 and f"dynamodb table {s.traces_table}" in names


# ---- everything a real run creates, a second run, a run right after clean-up ----

def inventory(s) -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    sns = boto3.client("sns", region_name=s.region)
    return {
        "tables": sorted(boto3.client("dynamodb", region_name=s.region).list_tables()["TableNames"]),
        "alarms": sorted(a["AlarmName"] for a in
                         boto3.client("cloudwatch", region_name=s.region).describe_alarms()["MetricAlarms"]),
        "topics": sorted(x["TopicArn"] for x in sns.list_topics()["Topics"]),
        "subscriptions": sorted(x["Endpoint"] for x in sns.list_subscriptions()["Subscriptions"]),
    }


def real_run_set_up(s) -> None:
    """What main.run, cache_run.py and trace_run.py --email create with --target aws."""
    from alarms import ensure_alarms

    for name, key, sk in ((s.traces_table, "request_id", None), (s.cache_table, "cache_key", None),
                          (s.spans_table, "trace_id", "span_id")):
        ensure_table(name, s.region, key=key, sort_key=sk)
    ensure_alarms(s.alarm_prefix, s.metric_namespace, s.topic_name, s.region, email="ana@example.com")


@mock_aws
def test_clean_up_leaves_nothing_and_a_run_right_after_it_works():
    from clean_up import clean_up, planned

    s = Settings()
    before = inventory(s)
    assert not any(before.values())
    real_run_set_up(s)
    first = inventory(s)
    assert all(first.values()), first  # every kind, the email subscription included
    assert sorted(first["tables"] + first["alarms"]) == sorted(n.split()[-1] for n in planned(s)[:6])
    real_run_set_up(s)  # a second run changes nothing
    assert inventory(s) == first
    assert not clean_up(s).failed
    assert inventory(s) == before
    real_run_set_up(s)  # a run right after clean-up
    assert inventory(s) == first
    clean_up(s)
    assert inventory(s) == before


@mock_aws
def test_clean_up_deletes_only_the_recipe_alarms_and_the_dry_run_lists_the_same_names():
    from clean_up import clean_up

    s = Settings()
    real_run_set_up(s)
    cw = boto3.client("cloudwatch", region_name=s.region)
    team = s.alarm_prefix + "p99"   # a team's own alarm with the recipe's prefix
    cw.put_metric_alarm(AlarmName=team, Namespace="team", MetricName="p99", Statistic="Maximum", Period=60,
                        EvaluationPeriods=1, Threshold=1.0, ComparisonOperator="GreaterThanThreshold")
    dry = clean_up(s, dry_run=True).removed
    steps = clean_up(s)
    assert steps.removed == dry and not steps.failed
    assert [a["AlarmName"] for a in cw.describe_alarms()["MetricAlarms"]] == [team]
    again = clean_up(s)   # a second clean-up: each alarm reported absent by its name
    assert [x for x in again.absent if x.startswith("cloudwatch alarm ")] == [
        f"cloudwatch alarm {s.alarm_name(slug)}" for slug in ("errors", "slow-answers", "daily-cost")]


@mock_aws
def test_run_right_after_clean_up_waits_for_the_deleting_table(engine, stub):
    s = Settings()
    real = boto3.client("dynamodb", region_name=s.region)
    ddb = StillDeleting(real, [s.traces_table])
    ensure_table(s.traces_table, s.region, key="request_id", client=ddb)
    assert ddb.waited == [("table_not_exists", s.traces_table)]
    assert real.describe_table(TableName=s.traces_table)["Table"]["TableStatus"] == "ACTIVE"  # created again
    traces = DynamoTraces(s.traces_table, s.region)
    a = answer("How many orders are there?", user_id="c-0007", model=stub, engine=engine, traces=traces)
    assert traces.get(a.request_id)["status"] == "answered"  # the trace is written, not lost


@mock_aws
def test_clean_up_skips_a_deleting_table_and_goes_on():
    from clean_up import clean_up

    s = Settings()
    real_run_set_up(s)
    ddb = StillDeleting(boto3.client("dynamodb", region_name=s.region), [s.traces_table])
    steps = clean_up(s, ddb=ddb)
    assert s.traces_table not in ddb.deletes and not steps.failed
    assert steps.absent == [f"dynamodb table {s.traces_table} (already being deleted)"]
    assert len(steps.removed) == 2 + 3 + 1  # the other tables, the alarms and the topic are still removed


@mock_aws
def test_clean_up_reports_a_refused_step_tries_the_rest_and_exits_1(monkeypatch, capsys):
    import pytest
    from botocore.exceptions import ClientError

    import clean_up as cu

    s = Settings()
    real_run_set_up(s)
    ddb = boto3.client("dynamodb", region_name=s.region)

    def refuse(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}}, "DescribeTable")

    monkeypatch.setattr(ddb, "describe_table", refuse)
    real_client = boto3.client
    monkeypatch.setattr(cu.boto3, "client", lambda name, **kw: ddb if name == "dynamodb" else real_client(name, **kw))
    monkeypatch.setattr("sys.argv", ["clean_up.py"])
    with pytest.raises(SystemExit) as e:
        cu.main()
    out = capsys.readouterr()
    assert e.value.code == 1 and out.err.count("FAILED dynamodb table") == 3
    assert "removed sns topic test-chat-alarms" in out.out and "3 resources removed" not in out.out
    left = inventory(s)
    assert len(left["tables"]) == 3 and not left["alarms"] and not left["topics"] and not left["subscriptions"]
