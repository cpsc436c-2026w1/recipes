"""Steps for the AWS paths, against moto: the trace table, clean-up. Every scenario that uses them is tagged @aws."""

from __future__ import annotations

import sys

import boto3
from behave import given, then, when
from botocore.exceptions import ClientError
from fakes import StillDeleting
from support import in_moto, inventory, patch, real_run_set_up, run_captured, set_env

from main.config import Settings
from main.infra import ensure_table
from main.traces import DynamoTraces


# ---- the trace table ----

@given('PREFIX is "{prefix}" and AWS_REGION is not set')
def prefix_and_no_region(context, prefix):
    set_env(context, "PREFIX", prefix)
    set_env(context, "AWS_REGION", None)


@when('a run with "--target aws" is set up, with the stub LLM in place of the gateway')
def aws_run_set_up(context):
    in_moto(context)
    import main.run as run

    patch(context, run, "ensure_data", lambda: context.paths)  # the scenario's Parquet files
    _engine, _model, context.traces, _version = run.build("aws", "stub", Settings())


@then('the DynamoDB table "{name}" is ACTIVE in "{region}"')
def table_is_active(context, name, region):
    table = boto3.client("dynamodb", region_name=region).describe_table(TableName=name)["Table"]
    assert table["TableStatus"] == "ACTIVE", table["TableStatus"]


@then('the table "{name}" is billed per request and has time to live on "{attribute}"')
def table_billing_and_ttl(context, name, attribute):
    ddb = boto3.client("dynamodb", region_name="ca-central-1")
    table = ddb.describe_table(TableName=name)["Table"]
    assert table["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST", table.get("BillingModeSummary")
    ttl = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    assert ttl["TimeToLiveStatus"] in ("ENABLED", "ENABLING") and ttl["AttributeName"] == attribute, ttl


@then('the table "{name}" holds the item of that request with status "{status}"')
def table_holds_item(context, name, status):
    table = boto3.resource("dynamodb", region_name="ca-central-1").Table(name)
    item = table.get_item(Key={"request_id": context.answer.request_id}).get("Item")
    assert item and item["status"] == status and "user_id" not in item, item


@given('the DynamoDB table "{name}" is in the DELETING state')
def table_is_deleting(context, name):
    in_moto(context)
    context.ddb = StillDeleting(boto3.client("dynamodb", region_name="ca-central-1"), [name])


@when('the recipe makes sure the table "{name}" exists')
def recipe_ensures_table(context, name):
    ensure_table(name, Settings().region, key="request_id", client=context.ddb)


@then('the recipe waited until the table "{name}" was gone')
def recipe_waited(context, name):
    assert context.ddb.waited == [("table_not_exists", name)], context.ddb.waited


@given('traces go to the DynamoDB table "{name}", which does not exist')
def traces_to_missing_table(context, name):
    in_moto(context)
    context.traces = DynamoTraces(name, Settings().region)


# ---- clean-up ----

@given('everything a real run creates exists for PREFIX "{prefix}"')
def everything_exists(context, prefix):
    in_moto(context)
    set_env(context, "PREFIX", prefix)
    real_run_set_up(Settings())
    context.ddb = None


@given('the team has its own alarm "{name}"')
def team_alarm(context, name):
    boto3.client("cloudwatch", region_name="ca-central-1").put_metric_alarm(
        AlarmName=name, Namespace="team", MetricName="p99", Statistic="Maximum", Period=60, EvaluationPeriods=1,
        Threshold=1.0, ComparisonOperator="GreaterThanThreshold")


@given("any call to AWS fails the scenario")
def no_aws_call(context):
    def refuse(*args, **kwargs):
        raise AssertionError("the dry run made an AWS client")

    patch(context, boto3, "client", refuse)


@given('AWS refuses every DescribeTable call with "{code}"')
def aws_refuses_describe_table(context, code):
    ddb = boto3.client("dynamodb", region_name="ca-central-1")

    def refuse(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message": "not allowed"}}, "DescribeTable")

    ddb.describe_table = refuse
    real_client = boto3.client
    patch(context, boto3, "client", lambda name, **kw: ddb if name == "dynamodb" else real_client(name, **kw))


@when("clean_up.py runs")
def clean_up_runs(context):
    in_moto(context)
    import clean_up as cu

    context.steps = cu.clean_up(Settings(), ddb=context.ddb)


@when("clean_up.py runs from the command line")
def clean_up_main(context):
    in_moto(context)
    import clean_up as cu

    patch(context, sys, "argv", ["clean_up.py"])
    run_captured(context, cu.main)


@when('clean_up.py runs from the command line with "{args}"')
def clean_up_main_with(context, args):
    in_moto(context)
    import clean_up as cu

    patch(context, sys, "argv", ["clean_up.py", *args.split()])
    run_captured(context, cu.main)


@then('every table, alarm and topic of the account has a name that starts with "{prefix}"')
def names_start_with(context, prefix):
    found = inventory("ca-central-1")
    names = found["tables"] + found["alarms"] + found["topics"]
    assert (len(found["tables"]), len(found["alarms"]), len(found["topics"])) == (3, 3, 1), found
    assert all(name.startswith(prefix) for name in names), names


@then("clean-up reports {removed:d} removed, {absent:d} absent and {failed:d} failed")
def clean_up_reports(context, removed, absent, failed):
    s = context.steps
    assert (len(s.removed), len(s.absent), len(s.failed)) == (removed, absent, failed), (s.removed, s.absent, s.failed)


@then('clean-up reports "{what}" as absent')
def clean_up_reports_absent(context, what):
    assert what in context.steps.absent, context.steps.absent


@then('clean-up did not ask DynamoDB to delete "{name}" again')
def clean_up_did_not_delete(context, name):
    assert name not in context.ddb.deletes, context.ddb.deletes


@then("the account has {tables:d} tables, {alarms:d} alarms, {topics:d} topics and {subscriptions:d} subscriptions")
def account_has(context, tables, alarms, topics, subscriptions):
    found = inventory("ca-central-1")
    assert [len(found[k]) for k in ("tables", "alarms", "topics", "subscriptions")] == [
        tables, alarms, topics, subscriptions], found


@then('the only alarm left is "{name}"')
def only_alarm_left(context, name):
    assert inventory("ca-central-1")["alarms"] == [name]
