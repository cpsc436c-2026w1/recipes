"""Steps for the scripts: main/run.py, the extension runs, clean_up.py, and what they leave in the account."""

from __future__ import annotations

import os

import boto3
from behave import given, then, when
from support import refuse_network, run_main

from config import Settings


def names(csv: str) -> list[str]:
    return sorted(t.strip() for t in csv.split(",") if t.strip())


def tables_now() -> list[str]:
    return sorted(boto3.client("dynamodb", region_name="ca-central-1").list_tables()["TableNames"])


def set_env(context, name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    context.undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))


@given('every network connection is refused')
def no_network(context):
    refuse_network(context)


@given('PREFIX is "{prefix}"')
def prefix_is(context, prefix):
    set_env(context, "PREFIX", prefix)


@given('AWS_REGION is "{region}"')
def region_is(context, region):
    set_env(context, "AWS_REGION", None if region == "not set" else region)


@given('the table "{name}" is in the DELETING state when the run starts')
def deleting_at_start(context, name):
    # DeleteTable has returned and the table is not gone yet: DescribeTable says DELETING once, then (moto) not found.
    context.calls.answer("DescribeTable", when=lambda p: p.get("TableName") == name, times=1,
                         reply={"Table": {"TableName": name, "TableStatus": "DELETING"}})
    context.deleting = name


@given('the table "{name}" is in the DELETING state')
def deleting(context, name):
    context.calls.answer("DescribeTable", when=lambda p: p.get("TableName") == name,
                         reply={"Table": {"TableName": name, "TableStatus": "DELETING"}})


@given('AWS refuses DeleteTable for "{name}" with "{code}"')
def refuse_delete(context, name, code):
    context.calls.answer("DeleteTable", when=lambda p: p.get("TableName") == name, error=code)


@given('the main scenario and both extensions have run for PREFIX "{prefix}"')
def everything_ran(context, prefix):
    assert Settings().prefix == prefix
    for rel in ("main/run.py", "ext-answer-cache/cache_run.py", "ext-traces/trace_run.py"):
        run_main(context, rel, ["--target", "aws"])
        assert context.exit_code == 0, context.printed_err
    context.printed = context.printed_err = ""
    context.calls.log.clear()


@given('the team has its own table "{name}"')
def own_table(context, name):
    boto3.client("dynamodb", region_name="ca-central-1").create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])


@when('{script} runs with "{args}"')
def script_runs(context, script, args):
    context.first_printed = context.printed
    run_main(context, script, args.split())


@when('{script} runs with "{args}" again')
def script_runs_again(context, script, args):
    context.first_printed = context.printed
    context.printed = ""
    run_main(context, script, args.split())


@when('clean_up.py runs')
def clean_up_runs(context):
    # the clean-up contract: a bare run only lists; a run that deletes passes --yes
    run_main(context, "clean_up.py", ["--yes"])


@when('clean_up.py runs again')
def clean_up_again(context):
    context.printed = context.printed_err = ""
    run_main(context, "clean_up.py", ["--yes"])


@then('the exit code is {code:d}')
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed, context.printed_err)


@then('the printed lines after the first are:')
def printed_lines(context):
    lines = context.printed.rstrip("\n").splitlines()
    assert lines[1:] == context.text.splitlines(), "\n".join(lines)


@then('the printed output holds "{text}"')
@then('the second run printed "{text}"')
@then('the printed output of the second clean-up holds "{text}"')
def printed_holds(context, text):
    assert text in context.printed, context.printed


@then('the error output holds "{text}"')
def error_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('the account has the tables "{csv}"')
def account_tables(context, csv):
    assert tables_now() == names(csv), tables_now()


@then('the account has no table')
def no_tables(context):
    assert tables_now() == [], tables_now()


@then('every table of the account is billed per request, has time to live on "{attr}" and the tag "{tag}"')
def table_settings(context, attr, tag):
    key, value = tag.split("=")
    c = boto3.client("dynamodb", region_name="ca-central-1")
    for name in tables_now():
        t = c.describe_table(TableName=name)["Table"]
        assert t["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST", name
        ttl = c.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
        assert ttl["TimeToLiveStatus"] == "ENABLED" and ttl["AttributeName"] == attr, (name, ttl)
        tags = c.list_tags_of_resource(ResourceArn=t["TableArn"])["Tags"]
        assert {"Key": key, "Value": value} in tags, (name, tags)


@then('every DynamoDB call went to "{region}"')
def calls_region(context, region):
    found = boto3.client("dynamodb", region_name=region).list_tables()["TableNames"]
    assert sorted(found) == ["test-chat-conversations", "test-chat-history"], (region, found)
    others = [r for r in ("ca-central-1", "us-west-2", "us-east-1") if r != region]
    for r in others:
        assert boto3.client("dynamodb", region_name=r).list_tables()["TableNames"] == [], r


@then('the run waited until "{name}" was gone and created it again')
def waited(context, name):
    described = [p for p in context.calls.of("DescribeTable") if p.get("TableName") == name]
    created = [p for p in context.calls.of("CreateTable") if p.get("TableName") == name]
    assert len(described) >= 2 and len(created) == 1, (len(described), len(created))
    deletes = context.calls.of("DeleteTable")
    assert deletes == [], deletes


@then('{operation} was called {n:d} times in all')
def called_n(context, operation, n):
    assert len(context.calls.of(operation)) == n, context.calls.of(operation)


@then('DeleteTable was not called')
def no_delete(context):
    assert context.calls.of("DeleteTable") == []


@then('DeleteTable was not called for "{name}"')
def no_delete_of(context, name):
    assert all(p["TableName"] != name for p in context.calls.of("DeleteTable"))
