"""Steps that run a script (dry, against the fake aws, against the local stand-in) and read what it printed."""

from __future__ import annotations

import json
import re

import support
from behave import given, then, when


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _extra(bucket=None, prefix=None, keep=False) -> dict:
    extra = {}
    if bucket:
        extra["BUCKET"] = bucket
    if prefix:
        extra["PREFIX"] = prefix
    if keep:
        extra["KEEP"] = "1"
    return extra


# ---- set-up of the fake aws and the environment

@given('the fake aws says every table is "{status}"')
def step_status(context, status):
    context.env["FAKE_AWS_TABLE_STATUS"] = status


@given('the fake aws refuses "{call}" with "{code}"')
def step_refuse(context, call, code):
    context.env |= {"FAKE_AWS_REFUSE": call, "FAKE_AWS_CODE": code}


@given('the fake aws refuses "{call}" with "{code}" from its call number {nth:d} on')
def step_refuse_from(context, call, code, nth):
    context.env |= {"FAKE_AWS_REFUSE": call, "FAKE_AWS_CODE": code, "FAKE_AWS_REFUSE_FROM": str(nth)}


@given('the fake aws ends the export with the status "{status}"')
def step_export_status(context, status):
    context.env["FAKE_AWS_EXPORT_STATUS"] = status


@given('the fake aws answers the last check "{check}" with "{name}"')
def step_left(context, check, name):
    context.env |= {"FAKE_AWS_LEFT_IN": check, "FAKE_AWS_LEFT": name}


@given('the fake aws ends every Athena query in the state "{state}"')
def step_athena_state(context, state):
    context.env["FAKE_AWS_ATHENA_STATE"] = state


@given("the fake aws returns an unprocessed item from the first {n:d} load calls")
def step_unprocessed_n(context, n):
    context.env["FAKE_AWS_UNPROCESSED"] = str(n)


@given("the fake aws returns an unprocessed item from every load call")
def step_unprocessed_always(context):
    context.env["FAKE_AWS_UNPROCESSED"] = "always"


@then("load call {second:d} sends only the item that load call {first:d} returned as unprocessed")
def step_resent(context, second, first):
    loads = [c for c in context.run.calls if "batch-write-item" in c]
    sent = support.request_items("aws " + loads[second - 1])
    assert sent == {"recipe-trips": [{"PutRequest": {"Item": {"trip_id": {"S": "t-0007"}}}}]}, sent
    assert len(next(iter(support.request_items("aws " + loads[first - 1]).values()))) == 25


@then('the recipe waited "{seconds}" seconds between the load calls of a batch')
def step_waited_seconds(context, seconds):
    waits = (context.tmp / "sleep.log").read_text().split()
    assert waits == _lst(seconds), waits


@given('MAX_UNITS is "{value}"')
def step_max_units(context, value):
    context.env["MAX_UNITS"] = value


@then('every printed command that holds "{a}" holds "{b}"')
def step_every_cmd(context, a, b):
    found = [c for c in context.run.cmds if a in c]
    assert found and all(b in c for c in found), found


@then("every index in a printed create-table command has a maximum of {r:d} read and {w:d} write request units")
def step_index_ceiling(context, r, w):
    for cmd in (c for c in context.run.cmds if "aws dynamodb create-table" in c and "--global-secondary-indexes" in c):
        indexes = json.JSONDecoder().raw_decode(cmd.split("--global-secondary-indexes ", 1)[1])[0]
        for index in indexes:
            assert index["OnDemandThroughput"] == {"MaxReadRequestUnits": r, "MaxWriteRequestUnits": w}, index


@given('AWS_REGION is "{value}"')
def step_region(context, value):
    context.region_set = None if value == "not set" else value


# ---- running a script

@when('"{script}" runs with DRY=1')
def step_dry(context, script):
    support.dry(context, script)


@when('"{script}" runs with DRY=1 and KEEP=1')
def step_dry_keep(context, script):
    support.dry(context, script, KEEP="1")


@when('"{script}" runs with DRY=1 and BUCKET "{bucket}"')
def step_dry_bucket(context, script, bucket):
    support.dry(context, script, BUCKET=bucket)


@when('"{script}" runs with DRY=1 and PREFIX "{prefix}"')
def step_dry_prefix(context, script, prefix):
    support.dry(context, script, PREFIX=prefix)


@when('"{script}" runs with DRY=1 and PREFIX ""')
def step_dry_no_prefix(context, script):
    support.dry(context, script)


def _fake(context, script, **extra):
    env = dict(extra)
    if hasattr(context, "region_set"):
        if context.region_set is None:        # AWS_REGION not set at all: the script's own default is what the calls carry
            return support.fake(context, script, unset=("AWS_REGION",), **env)
        env["AWS_REGION"] = context.region_set
    return support.fake(context, script, **env)


@when('"{script}" runs against the fake aws')
def step_fake(context, script):
    _fake(context, script)


@when('"{script}" runs against the fake aws with KEEP=1')
def step_fake_keep(context, script):
    _fake(context, script, KEEP="1")


@when('"{script}" runs against the fake aws with BUCKET "{bucket}"')
def step_fake_bucket(context, script, bucket):
    _fake(context, script, BUCKET=bucket)


@when('"{script}" runs against the fake aws with BUCKET "{bucket}" and KEEP=1')
def step_fake_bucket_keep(context, script, bucket):
    _fake(context, script, BUCKET=bucket, KEEP="1")


@given('main/run.sh has run against the local stand-in with KEEP=1 and PREFIX "{prefix}"')
def step_main_ran(context, prefix):
    context.prefix = prefix
    run = support.shared_run(context, "main", "main/run.sh", KEEP="1", PREFIX=prefix)
    assert run.code == 0, run.out[-1500:] + run.err[-1500:]
    context.main = run


@given("ext-riders-and-drivers/run.sh has run after it with KEEP=1")
def step_riders_ran(context):
    run = support.shared_run(context, "riders", "ext-riders-and-drivers/run.sh", KEEP="1", PREFIX=context.prefix)
    assert run.code == 0, run.out[-1500:] + run.err[-1500:]


@when('"{script}" runs against the local stand-in with PREFIX "{prefix}"')
def step_local(context, script, prefix):
    support.local(context, script, PREFIX=prefix)


@when('"{script}" runs against the local stand-in with KEEP=1 and PREFIX "{prefix}"')
def step_local_keep(context, script, prefix):
    support.local(context, script, PREFIX=prefix, KEEP="1")


# ---- what a run printed and called

@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.run.code == code, f"exit code {context.run.code}\n{context.run.out[-1500:]}\n{context.run.err[-1500:]}"


@then("the exit code is not 0")
def step_code_not_0(context):
    assert context.run.code != 0, context.run.out[-1500:]


@then("aws was not called")
def step_not_called(context):
    assert "aws was called" not in context.run.err, context.run.err


@then("aws was called {n:d} times")
def step_called_n(context, n):
    assert len(context.run.calls) == n, context.run.calls


@then('at least {n:d} commands that start with "{start}" are printed')
def step_at_least(context, n, start):
    found = [c for c in context.run.cmds if c.startswith(start)]
    assert len(found) >= n, f"{len(found)} commands:\n" + "\n".join(c[:100] for c in found)


@then('each of the steps "{steps}" is printed with at least one command under it')
def step_every_step(context, steps):
    for n in _lst(steps):
        assert context.run.step_cmds(n), f"step {n} printed no command:\n{context.run.step(n)}"


@then('step "{n}", the refused accept, printed no units and says that a refused write reports none')
def step_refused_no_units(context, n):
    answer = "\n".join(line for line in context.run.step(n).splitlines() if not line.startswith("$ "))
    assert "write_units" not in answer and "CapacityUnits" not in answer, answer
    assert "A refused write reports no units" in answer, answer


@then('the heading of step "{n}" holds "{text}"')
def step_heading(context, n, text):
    (heading,) = re.findall(rf"^== {re.escape(n)}\. (.*)$", context.run.out, flags=re.M)
    assert text in heading, heading


@then('the printed commands end with the deletes of the tables "{tables}" and the two last checks')
def step_ext_ends_with_clean_up(context, tables):
    cmds = context.run.cmds
    deletes = [c for c in cmds if "delete-table --table-name" in c]
    assert deletes == [f"aws dynamodb delete-table --table-name {t} --query TableDescription.TableStatus"
                       for t in _lst(tables)], deletes
    assert cmds[-2].startswith("aws dynamodb list-tables") and cmds[-1].startswith("aws glue get-databases"), cmds[-2:]
    scenario = [i for i, c in enumerate(cmds) if "transact-write-items" in c or c.startswith("athena:")
                or "update-item" in c]
    assert cmds.index(deletes[0]) > max(scenario), "the clean-up is not after the scenario's last command"


@then('the output holds "{text}"')
def step_out_holds(context, text):
    assert text in context.run.out, context.run.out[-2500:]


@then('the output does not hold "{text}"')
def step_out_not(context, text):
    assert text not in context.run.out, context.run.out[-2500:]


@then('the error output holds "{text}"')
def step_err_holds(context, text):
    assert text in context.run.err, context.run.err[-2500:]


@then('the error output does not hold "{text}"')
def step_err_not(context, text):
    assert text not in context.run.err, context.run.err[-2500:]


@then('{n:d} printed lines hold "{text}"')
def step_lines_hold(context, n, text):
    found = [line for line in context.run.out.splitlines() if text in line]
    assert len(found) == n, f"{len(found)} lines:\n" + "\n".join(found)


@then('no printed command holds "{text}"')
def step_no_cmd(context, text):
    assert not [c for c in context.run.cmds if text in c]


@then('{n:d} printed command holds "{text}"')
@then('{n:d} printed commands hold "{text}"')
def step_n_cmds(context, n, text):
    found = [c for c in context.run.cmds if text in c]
    assert len(found) == n, context.run.cmds


@then('no call holds "{text}"')
def step_no_call(context, text):
    assert not [c for c in context.run.calls if text in c], context.run.calls


@then('{n:d} call holds "{text}"')
@then('{n:d} calls hold "{text}"')
def step_n_calls(context, n, text):
    found = [c for c in context.run.calls if text in c]
    assert len(found) == n, f"{len(found)} calls hold {text!r}:\n" + "\n".join(c[:160] for c in context.run.calls)


@then('every call that holds "{a}" holds "{b}"')
def step_every_call(context, a, b):
    found = [c for c in context.run.calls if a in c]
    assert found and all(b in c for c in found), found


@then('the call "{a}" comes before the call "{b}"')
def step_before(context, a, b):
    calls = context.run.calls
    first = next((i for i, c in enumerate(calls) if a in c), None)
    second = next((i for i, c in enumerate(calls) if b in c), None)
    assert first is not None and second is not None and first < second, "\n".join(c[:160] for c in calls)


@then('the only call is a describe-table of "{table}"')
def step_only_describe(context, table):
    calls = context.run.calls
    assert len(calls) == 1 and "describe-table" in calls[0] and f"--table-name {table} " in calls[0] + " ", calls


@then("every call to aws other than the looks at the table and the waits is one of the printed commands")
def step_calls_printed(context):
    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    printed = [norm(c.removeprefix("aws ")) for c in context.run.cmds]
    context.scenario_calls = [norm(c) for c in context.run.calls
                              if "describe-table" not in c and " wait " not in f" {c} "
                              and not c.startswith("configure list")]      # the look at the AWS CLI itself
    assert context.scenario_calls, context.run.calls
    for call in context.scenario_calls:
        assert call in printed, f"called and not printed: {call[:200]}"


@then("the printed commands are in the order of the calls")
def step_order(context):
    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    printed = [norm(c.removeprefix("aws ")) for c in context.run.cmds]
    assert printed == context.scenario_calls, "the printed commands and the calls differ in number or order"


@then('the last printed commands delete the tables "{tables}" and check that none is left')
def step_ends_with_clean_up(context, tables):
    cmds = context.run.cmds
    deletes = [c for c in cmds if "delete-table --table-name" in c]
    assert deletes == [f"aws dynamodb delete-table --table-name {t} --query TableDescription.TableStatus"
                       for t in _lst(tables)], deletes
    assert cmds[-2].startswith("aws dynamodb list-tables") and cmds[-1].startswith("aws glue get-databases"), cmds[-2:]
    assert cmds.index(deletes[0]) > max(i for i, c in enumerate(cmds) if "dynamodb scan" in c)


@then('every table the printed commands name starts with "{start}"')
def step_names(context, start):
    names = set(re.findall(r"--table-name (\S+)", context.run.out))
    names |= set(re.findall(r'"TableName": "([^"]+)"', context.run.out))
    assert names and all(n.startswith(start) for n in names), names


@then('every call to aws was made with AWS_REGION "{region}"')
def step_regions(context, region):
    assert context.run.code == 0, context.run.err
    assert context.run.regions and set(context.run.regions) == {region}, set(context.run.regions)


@then("every other step of the clean-up was still called")
def step_all_steps(context):
    for step in ("delete-table --table-name recipe-trips ", "delete-table --table-name recipe-rider-trips ",
                 "delete-table --table-name recipe-drivers ", "glue delete-table --database-name recipe_lake",
                 "glue delete-database --name recipe_lake", "s3 rm --recursive --quiet s3://my-bucket/recipe/exports/",
                 "s3 rm --recursive --quiet s3://my-bucket/recipe/athena-results/", "dynamodb list-tables",
                 "glue get-databases"):
        assert any(step in c for c in context.run.calls), f"not called: {step}"


@then('the call after each "dynamodb delete-table" is a "wait table-not-exists" for the same table')
def step_waited(context):
    calls = context.run.calls
    deleted = [(i, re.search(r"--table-name (\S+)", c).group(1)) for i, c in enumerate(calls)
               if "dynamodb delete-table" in c]
    assert len(deleted) == 3, calls
    for i, table in deleted:
        assert f"dynamodb wait table-not-exists --table-name {table}" in calls[i + 1], calls[i + 1]
