"""Steps that run a script (dry, or for real against the stand-in aws) and read what it printed and called."""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from datetime import datetime

import support
from behave import given, then, when
from harness import RECIPE, script_env


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(", ") if t.strip()]


# ---- set-up

@given('ROLE_ARN is "{arn}"')
def step_role_arn(context, arn):
    context.env["ROLE_ARN"] = arn


@given('LOG_DAYS is "{days}"')
def step_log_days(context, days):
    context.env["LOG_DAYS"] = days


@given("the fake aws says the API of an earlier run is there")
def step_exists(context):
    context.env["FAKE_AWS_EXISTS"] = "1"


@given("the fake aws refuses the first {n:d} add-permission calls as in progress")
def step_busy(context, n):
    context.env["FAKE_AWS_BUSY"] = str(n)


@given('the fake aws refuses "{call}" with "{code}" and the message "{message}"')
def step_refuse(context, call, code, message):
    context.env |= {"FAKE_AWS_REFUSE": call, "FAKE_AWS_CODE": code, "FAKE_AWS_MESSAGE": message}


@given("the fake aws says the tables of the dynamodb recipe are not found")
def step_no_tables(context):
    context.env["FAKE_AWS_NO_TABLES"] = "1"


@given('the fake aws answers the last check "{check}" with "{name}"')
def step_left(context, check, name):
    context.env |= {"FAKE_AWS_LEFT_IN": check, "FAKE_AWS_LEFT": name}


@given('AWS_REGION is "{value}"')
def step_region(context, value):
    context.region_set = None if value == "not set" else value


# ---- running

def _fake(context, script, **extra):
    if getattr(context, "region_set", "x") is None:
        return support.fake(context, script, unset=("AWS_REGION",), **extra)
    if hasattr(context, "region_set"):
        extra["AWS_REGION"] = context.region_set
    return support.fake(context, script, **extra)


@when('"{script}" runs against the fake aws')
def step_fake(context, script):
    _fake(context, script)


@when('"{script}" runs against the fake aws with KEEP=1')
def step_fake_keep(context, script):
    _fake(context, script, KEEP="1")


@when('"{script}" runs against the fake aws with PREFIX "{prefix}"')
def step_fake_prefix(context, script, prefix):
    _fake(context, script, PREFIX=prefix)


@when('"{script}" runs with DRY=1')
def step_dry(context, script):
    support.dry(context, script)


# ---- what a run printed and called

@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.run.code == code, f"exit code {context.run.code}\n{context.run.out[-1500:]}\n{context.run.err[-1500:]}"


@then("the exit code is not 0")
def step_code_not_0(context):
    assert context.run.code != 0, context.run.out[-1500:]


@then("neither aws nor curl was called")
def step_not_called(context):
    assert "was called under DRY=1" not in context.run.err, context.run.err


@then('at least {n:d} commands that start with "{start}" are printed')
def step_at_least(context, n, start):
    found = [c for c in context.run.cmds if c.startswith(start)]
    assert len(found) >= n, f"{len(found)} commands:\n" + "\n".join(c[:100] for c in found)


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


@then('no call holds "{text}"')
def step_no_call(context, text):
    assert not [c for c in context.run.calls if text in c], [c[:150] for c in context.run.calls if text in c]


@then('{n:d} call holds "{text}"')
@then('{n:d} calls hold "{text}"')
def step_n_calls(context, n, text):
    found = [c for c in context.run.calls if text in c]
    assert len(found) == n, f"{len(found)} calls hold {text!r}:\n" + "\n".join(c[:160] for c in context.run.calls)


@then('the call that holds "{a}" holds each of "{parts}"')
def step_call_holds(context, a, parts):
    (call,) = [c for c in context.run.calls if a in c]
    for part in _lst(parts):
        assert f" {part} " in f" {call} ", f"{part!r} is not in: {call[:400]}"


@then('the last call holds "{text}"')
def step_last_call(context, text):
    assert text in context.run.calls[-1], context.run.calls[-1]


@then('the call "{a}" comes before the call "{b}"')
def step_before(context, a, b):
    calls = context.run.calls
    first = next((i for i, c in enumerate(calls) if a in c), None)
    second = next((i for i, c in enumerate(calls) if b in c), None)
    assert first is not None and second is not None and first < second, "\n".join(c[:160] for c in calls)


@then('the only call is a describe-table of "{table}"')
def step_only_describe(context, table):
    calls = context.run.calls
    assert len(calls) == 1 and "describe-table" in calls[0] and f"--table-name {table} " in calls[0], calls


@then('the zip the script made holds only "{name}", the same bytes as main/handler.py')
def step_zip(context, name):
    (path,) = list(context.tmp.glob("*-api.zip"))
    with zipfile.ZipFile(path) as z:
        assert z.namelist() == [name], z.namelist()
        assert z.read(name) == (RECIPE / "main" / "handler.py").read_bytes()


@then('the role\'s policy allows on DynamoDB only the actions "{actions}"')
def step_actions(context, actions):
    context.policy = support.policy_of(context.run)
    found = [s for s in context.policy["Statement"] if any(a.startswith("dynamodb:") for a in s["Action"])]
    assert len(found) == 1 and found[0]["Effect"] == "Allow" and found[0]["Action"] == _lst(actions), found


@then('the role\'s policy names on DynamoDB only the tables "{tables}"')
def step_tables(context, tables):
    (statement,) = [s for s in context.policy["Statement"] if any(a.startswith("dynamodb:") for a in s["Action"])]
    assert [r.rsplit(":table/", 1)[1] for r in statement["Resource"]] == _lst(tables), statement["Resource"]


@then('the role\'s policy allows on logs only the log group "{group}"')
def step_logs(context, group):
    (statement,) = [s for s in context.policy["Statement"] if any(a.startswith("logs:") for a in s["Action"])]
    assert statement["Resource"].endswith(f":log-group:{group}:*"), statement
    assert statement["Action"] == ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], statement


@then('the role\'s policy has no action and no resource that is "*"')
def step_no_wildcard(context):
    assert len(context.policy["Statement"]) == 2
    for s in context.policy["Statement"]:
        resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        assert not any("*" in a for a in s["Action"]) and "*" not in resources, s


@then('the role\'s trust policy lets only "{service}" use it')
def step_trust(context, service):
    (call,) = [c for c in context.run.calls if "iam create-role" in c]
    trust = json.JSONDecoder().raw_decode(call.split("--assume-role-policy-document ", 1)[1])[0]
    assert trust["Statement"] == [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]


@then('curl was called {n:d} times, each with a POST to "{url}"')
def step_curls(context, n, url):
    assert len(context.run.curls) == n, context.run.curls
    assert all(f"-X POST {url} " in c for c in context.run.curls), context.run.curls


@then('the actions of the curl calls are "{actions}"')
def step_curl_actions(context, actions):
    found = [json.loads(c.split(" -d ", 1)[1])["action"] for c in context.run.curls]
    assert found == _lst(actions), found


@then('the deletes are, in this order, "{deletes}"')
def step_deletes(context, deletes):
    found = [" ".join(c.split()[:2]) for c in context.run.calls if c.split()[1].startswith("delete-")]
    assert found == _lst(deletes), found


@then('every delete names "{fn}", "{group}" or "{role}"')
def step_delete_names(context, fn, group, role):
    for c in (c for c in context.run.calls if c.split()[1].startswith("delete-")):
        name = re.search(r"--(?:function-name|log-group-name|role-name) (\S+)", c).group(1)
        assert name in (fn, group, role), c


@then("all 5 deletes and the 3 last checks were still called")
def step_all_deletes(context):
    for part in ("lambda delete-function-url-config", "lambda delete-function ", "logs delete-log-group",
                 "iam delete-role-policy", "iam delete-role ", "logs describe-log-groups", "iam list-roles",
                 "lambda list-functions"):
        assert any(part in c for c in context.run.calls), f"not called: {part}"


@then("the recipe waited {seconds:d} seconds before each of the {n:d} new attempts")
def step_waits(context, seconds, n):
    # ROLE_WAIT (10), then one wait per new attempt; the later sleeps are those of steps 8 and 9
    assert context.run.sleeps[:1 + n] == ["10"] + [str(seconds)] * n, context.run.sleeps


@then('every function, role and log group the calls name is one of "{names}"')
def step_names(context, names):
    found = set(re.findall(r"--(?:function-name|role-name|log-group-name) (\S+)", "\n".join(context.run.calls)))
    assert found == set(_lst(names)), found


@then('every call to aws was made with AWS_REGION "{region}"')
def step_regions(context, region):
    assert context.run.code == 0, context.run.err
    assert context.run.regions and set(context.run.regions) == {region}, set(context.run.regions)


@then('every ARN in the role\'s policy is in the Region "{region}"')
def step_policy_region(context, region):
    policy = support.policy_of(context.run)
    arns = [r for s in policy["Statement"] for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    assert len(arns) == 4 and all(a.split(":")[3] == region for a in arns), arns


@then('the metrics the script asks for are "{metrics}", each for the function "{fn}"')
def step_metrics(context, metrics, fn):
    calls = [c for c in context.run.calls if "cloudwatch get-metric-statistics" in c]
    assert [re.search(r"--metric-name (\S+)", c).group(1) for c in calls] == _lst(metrics), calls
    assert all(f"--namespace AWS/Lambda " in c and f"Name=FunctionName,Value={fn} " in c for c in calls), calls


@then("each metric call covers the {seconds:d} seconds before now, as one period")
def step_hour(context, seconds):
    for c in (c for c in context.run.calls if "cloudwatch get-metric-statistics" in c):
        start = datetime.strptime(re.search(r"--start-time (\S+)", c).group(1), "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(re.search(r"--end-time (\S+)", c).group(1), "%Y-%m-%dT%H:%M:%SZ")
        assert 0 <= (end - start).total_seconds() - seconds <= 2, (start, end)
        assert abs((datetime.utcnow() - end).total_seconds()) < 120, end
        assert f"--period {seconds} " in c, c


@then('every call is one of "{kinds}"')
def step_only_reads(context, kinds):
    assert context.run.calls
    for c in context.run.calls:
        assert " ".join(c.split()[:2]) in _lst(kinds), c


@then("python3 ran main/handler.py once, before the first call that creates something")
def step_handler_first(context):
    assert len(context.run.handler_runs) == 1 and context.run.handler_runs[0].endswith("main/handler.py")
    out = context.run.out
    assert out.index("python3 ") < out.index("$ aws iam create-role"), "the handler ran after the role was created"


@then('the output of step "{n}" holds "{text}"')
def step_step_holds(context, n, text):
    assert text in context.run.step(n), context.run.step(n)


# ---- the local run against a moto server

@given('the dynamodb recipe\'s main and riders-and-drivers scripts have run with KEEP=1 and PREFIX "{prefix}"')
def step_dynamodb_recipe(context, prefix):
    env = script_env(context.tmp) | {
        "AWS_ENDPOINT_URL": context.moto_url, "AWS_REGION": "ca-central-1", "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "KEEP": "1", "PREFIX": prefix}
    context.local_env = env
    for script in ("main/run.sh", "ext-riders-and-drivers/run.sh"):
        p = subprocess.run(["bash", str(RECIPE.parent / "dynamodb" / script)], env=env, capture_output=True, text=True,
                           timeout=240)
        assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-1500:]


@when('main/run.sh runs with PREFIX "{prefix}" and no Region set, its step 1 against the moto server and its aws '
      "a stand-in")
def step_local_handler(context, prefix):
    import sys

    from harness import FAKE_AWS, LOGGING, put_tools

    # The script itself runs. Its `aws`, `curl` and `sleep` are the stand-ins of tests/harness.py, so nothing is
    # created anywhere; its python3 is the real one, so step 1 runs main/handler.py, whose boto3 goes to the moto
    # server (AWS_ENDPOINT_URL). No Region is set: the handler gets the one lib.sh exports.
    real_python = f"#!/bin/sh\nexec {sys.executable} \"$@\"\n"
    bin_dir = put_tools(context.tmp / "bin", aws=FAKE_AWS, curl=LOGGING, sleep=LOGGING, python3=real_python)
    log = context.tmp / "aws.log"
    env = {k: v for k, v in context.local_env.items() if k not in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    env |= {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(log), "PREFIX": prefix, "KEEP": "1",
            "TOOL_LOG": str(context.tmp / "tool.log")}
    p = subprocess.run(["bash", str(RECIPE / "main" / "run.sh")], env=env, capture_output=True, text=True, timeout=180)
    context.run = support.Run("main/run.sh", p.returncode, p.stdout, p.stderr, log.read_text().splitlines())
    context.lines = [line.split(" -> ", 1) for line in context.run.step("1").splitlines() if " -> " in line]


@then("step 1 printed {n:d} lines, one per event, before the first call that creates something")
def step_five(context, n):
    assert len(context.lines) == n, context.run.out[:3000] + context.run.err[-1500:]
    out = context.run.out
    assert out.index(" -> ") < out.index("$ aws iam create-role"), "the handler ran after the role was created"


@then('step "{n}" prints {count:d} commands, each "{start}" with one of the actions "{actions}"')
def step_curl_lines(context, n, count, start, actions):
    printed = [c for c in support.commands(context.run.step(n))]
    assert len(printed) == count and all(c.startswith(start) for c in printed), printed
    assert [json.loads(c.split(" -d ", 1)[1].strip("'"))["action"] for c in printed] == _lst(actions), printed


@then('the steps "{steps}" are all printed')
def step_steps_printed(context, steps):
    for n in _lst(steps):
        context.run.step(n)


@then('printed line {n:d} answers "{action}" with trip "{trip}" held by "{driver}"')
def step_line_get(context, n, action, trip, driver):
    event, reply = (json.loads(part) for part in context.lines[n - 1])
    assert event["action"] == action and reply["item"]["trip_id"] == trip and reply["item"]["driver_id"] == driver, reply


@then('printed line {n:d} answers "{action}" with every trip of "{rider}" from "{since}" on that the table holds, in time order')
def step_line_rider(context, n, action, rider, since):
    import boto3

    event, reply = (json.loads(part) for part in context.lines[n - 1])
    table = boto3.client("dynamodb", region_name="ca-central-1", endpoint_url=context.moto_url,
                         aws_access_key_id="testing", aws_secret_access_key="testing")
    held = sorted(i["requested_at"]["S"] for i in table.scan(TableName=f"{context.local_env['PREFIX']}-rider-trips",
                                                             ConsistentRead=True)["Items"]
                  if i["rider_id"]["S"] == rider and i["requested_at"]["S"] >= since)
    found = [i["requested_at"] for i in reply["items"]]
    assert event["action"] == action and held and found == held, (found, held)
    assert all(i["rider_id"] == rider for i in reply["items"]), reply


@then('printed line {n:d} is the answer {reply} to the action "{action}"')
def step_line_reply(context, n, reply, action):
    event, found = (json.loads(part) for part in context.lines[n - 1])
    assert event["action"] == action and found == json.loads(reply), found
