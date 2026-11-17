"""Steps for main/run.py, main/fire.py, clean_up.py and the function, against moto."""

from __future__ import annotations

import json
import os

import boto3
from behave import given, then, when
from support import run_main

from config import Settings

TAG = {"Key": "recipe", "Value": "limits-and-alarms"}


def c(service: str, region: str | None = None):
    return boto3.client(service, region_name=region or ("us-east-1" if service == "budgets" else "ca-central-1"))


def account() -> str:
    return c("sts").get_caller_identity()["Account"]


def set_env(context, name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    context.undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))


def csv(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


# ---- Given ----

@given('a run with "--target aws" has set up everything for PREFIX "{prefix}"')
def set_up_everything(context, prefix):
    assert Settings().prefix == prefix
    run_main(context, "main/run.py", ["--target", "aws"])
    assert context.exit_code == 0, context.printed_err
    context.printed = context.printed_err = ""
    context.calls.log.clear()
    context.invocations.clear()


@given('PREFIX is "{prefix}"')
def prefix_is(context, prefix):
    set_env(context, "PREFIX", prefix)


@given('AWS_REGION is "{region}"')
def region_is(context, region):
    set_env(context, "AWS_REGION", None if region == "not set" else region)


@given('ALARM_EMAIL is "{email}"')
def email_is(context, email):
    set_env(context, "ALARM_EMAIL", None if email == "not set" else email)


@given('the account reports a total of {total:d} with {unreserved:d} unreserved')
def account_settings(context, total, unreserved):
    context.calls.answer("GetAccountSettings", reply={"AccountLimit": {
        "ConcurrentExecutions": total, "UnreservedConcurrentExecutions": unreserved}})


@given('the team has its own lambda function "{name}" without the tag')
def own_function(context, name):
    role = c("iam").create_role(RoleName="team-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    c("lambda").create_function(FunctionName=name, Runtime="python3.13", Role=role, Handler="x.handler",
                                Code={"ZipFile": b"PK\x05\x06" + b"\x00" * 18})


@given('the team has its own budget "{name}" without the tag')
def own_budget(context, name):
    c("budgets").create_budget(AccountId=account(), Budget={
        "BudgetName": name, "BudgetLimit": {"Amount": "10", "Unit": "USD"}, "TimeUnit": "MONTHLY",
        "BudgetType": "COST"})


@given('the team has its own sns topic "{name}" without the tag')
def own_topic(context, name):
    c("sns").create_topic(Name=name)


@given('the team has its own iam role "{name}" without the tag')
def own_role(context, name):
    c("iam").create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "lambda.amazonaws.com"}}]}))
    c("iam").put_role_policy(RoleName=name, PolicyName="anything", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}))


@given('the team\'s role "{name}" exists and LAMBDA_ROLE_ARN names it')
def team_role(context, name):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "lambda.amazonaws.com"}}]}
    arn = c("iam").create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    set_env(context, "LAMBDA_ROLE_ARN", arn)


@given('every Invoke of the function ends with FunctionError "{error}"')
def function_breaks(context, error):
    import io

    from botocore.response import StreamingBody

    def broken(client, params):
        body = b'{"errorMessage": "Unable to import module app", "errorType": "Runtime.ImportModuleError"}'
        return {"StatusCode": 200, "FunctionError": error, "Payload": StreamingBody(io.BytesIO(body), len(body))}
    context.calls.answer("Invoke", fn=broken)


@given('the alarm history of each alarm shows its SNS action')
def alarm_history(context):
    context.calls.answer("DescribeAlarmHistory", reply={"AlarmHistoryItems": [{
        "HistoryItemType": "Action", "HistorySummary": "Successfully executed action arn:aws:sns:..."}]})


@given('the function "{name}" has lost the tag "{key}"')
def function_untagged(context, name, key):
    arn = c("lambda").get_function(FunctionName=name)["Configuration"]["FunctionArn"]
    c("lambda").untag_resource(Resource=arn, TagKeys=[key])


@given('the role "{name}" has lost the tag "{key}"')
def role_untagged(context, name, key):
    c("iam").untag_role(RoleName=name, TagKeys=[key])


@given('AWS refuses {operation} with "{code}"')
def refuse(context, operation, code):
    context.calls.answer(operation, error=code)


@given('the team has its own alarm "{name}"')
def own_alarm(context, name):
    c("cloudwatch").put_metric_alarm(AlarmName=name, Namespace="test/app", MetricName="LatencyMs",
                                     ExtendedStatistic="p99", Period=60, EvaluationPeriods=1, Threshold=9000,
                                     ComparisonOperator="GreaterThanThreshold")


# ---- When ----

@when('{script} runs with "{args}"')
def script_runs(context, script, args):
    context.calls.mark = len(context.calls.log)
    run_main(context, script, args.split())


@when('clean_up.py runs')
def clean_up_runs(context):
    run_main(context, "clean_up.py", [])


@when('clean_up.py runs again')
def clean_up_again(context):
    context.printed = context.printed_err = ""
    run_main(context, "clean_up.py", [])


@when('the function is invoked with {payload}')
def invoke(context, payload):
    context.invocations.clear()
    r = c("lambda").invoke(FunctionName=Settings().function_name, Payload=payload.encode())
    context.answer = json.loads(r["Payload"].read())
    context.inv = context.invocations[-1]
    context.line = json.loads(context.inv["lines"][-1]) if context.inv["lines"] else None


# ---- Then ----

@then('the exit code is {code:d}')
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed[-2000:], context.printed_err[-2000:])


@then('the printed output holds "{text}"')
@then('the printed output of the second clean-up holds "{text}"')
def printed_holds(context, text):
    assert text in context.printed, context.printed


@then('the error output holds "{text}"')
def error_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('the printed output has {n:d} lines with "{text}"')
def printed_n(context, n, text):
    assert sum(text in ln for ln in context.printed.splitlines()) == n, context.printed


@then('the function answers {code:d}')
def answers(context, code):
    assert context.answer["statusCode"] == code, context.answer


@then('the invocation wrote {n:d} log line')
def log_lines(context, n):
    assert len(context.inv["lines"]) == n, context.inv["lines"]


@then('the line declares the metrics "{metrics}" in the namespace "{ns}" with the dimensions "{dims}"')
def line_declares(context, metrics, ns, dims):
    d = context.line["_aws"]["CloudWatchMetrics"]
    assert len(d) == 1 and d[0]["Namespace"] == ns, d
    assert [m["Name"] for m in d[0]["Metrics"]] == csv(metrics), d
    assert d[0]["Dimensions"] == [csv(dims)], d


@then('the line has Service "{service}", Requests {r:d}, Errors {e:d}, TokensIn {tin:d} and TokensOut {tout:d}')
def line_values(context, service, r, e, tin, tout):
    got = {k: context.line[k] for k in ("Service", "Requests", "Errors", "TokensIn", "TokensOut")}
    assert got == {"Service": service, "Requests": r, "Errors": e, "TokensIn": tin, "TokensOut": tout}, got


@then("the line's requestId is the invocation's request id, as a plain field")
def request_id(context):
    rid = context.line["requestId"]
    assert len(rid) == 36, rid
    assert all(rid not in json.dumps(d) for d in context.line["_aws"]["CloudWatchMetrics"])


@then('the line has Errors {e:d} and errorType "{error_type}"')
def line_error(context, e, error_type):
    assert (context.line["Errors"], context.line.get("errorType")) == (e, error_type), context.line


@then('the line has CostCAD 0, TokensIn 0 and TokensOut 0')
def line_zero(context):
    assert (context.line["CostCAD"], context.line["TokensIn"], context.line["TokensOut"]) == (0, 0, 0), context.line


@then('the line has CostCAD {cost:g}')
def line_cost(context, cost):
    assert abs(context.line["CostCAD"] - cost) < 1e-12, context.line["CostCAD"]


@then('every log line of the invocation is one JSON object with nothing before or after it')
def one_json(context):
    assert context.inv["lines"]
    for ln in context.inv["lines"]:
        assert ln.startswith("{") and ln.endswith("}") and isinstance(json.loads(ln), dict), ln


@then('the budget "{name}" is {amount} USD a month, of cost')
def budget_is(context, name, amount):
    b = c("budgets").describe_budget(AccountId=account(), BudgetName=name)["Budget"]
    assert (b["BudgetLimit"]["Amount"], b["BudgetLimit"]["Unit"], b["TimeUnit"], b["BudgetType"]) == (
        amount, "USD", "MONTHLY", "COST"), b


@then('the budget\'s alerts are "{alerts}", each sent to the topic "{topic}"')
def budget_alerts(context, alerts, topic):
    sent = context.calls.of("CreateBudget")[-1]["NotificationsWithSubscribers"]
    got = [f"{n['Notification']['NotificationType']} {n['Notification']['Threshold']:g}" for n in sent]
    assert got == csv(alerts), got
    for n in sent:
        assert n["Notification"]["ComparisonOperator"] == "GREATER_THAN", n
        assert n["Notification"]["ThresholdType"] == "PERCENTAGE", n
        assert [s["Address"].split(":")[-1] for s in n["Subscribers"]] == [topic], n


@then('the function "{name}" has a timeout of {s:d} seconds')
def timeout(context, name, s):
    assert c("lambda").get_function_configuration(FunctionName=name)["Timeout"] == s


@then('the reserved concurrency of "{name}" is {value}')
def reserved(context, name, value):
    got = c("lambda").get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
    assert got == (None if value == "none" else int(value)), got


@then('{operation} was not called')
def not_called(context, operation):
    assert context.calls.since_mark(operation) == [], context.calls.since_mark(operation)


@then('the role "{name}" may be assumed only by "{service}"')
def role_trust(context, name, service):
    doc = c("iam").get_role(RoleName=name)["Role"]["AssumeRolePolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    principals = [st["Principal"] for st in doc["Statement"]]
    assert principals == [{"Service": service}], principals


@then('the role\'s only permissions are "{actions}" on the log group "{group}"')
def role_permissions(context, actions, group):
    iam = c("iam")
    role = Settings().role_name
    assert iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"] == []
    names = iam.list_role_policies(RoleName=role)["PolicyNames"]
    assert len(names) == 1, names
    doc = iam.get_role_policy(RoleName=role, PolicyName=names[0])["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    got = set()
    arn = f"arn:aws:logs:ca-central-1:{account()}:log-group:{group}"
    for st in doc["Statement"]:
        assert st["Effect"] == "Allow" and st["Resource"] in (arn, f"{arn}:*"), st
        got |= set([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
    assert sorted(got) == sorted(csv(actions)), got


@then('the function "{name}" runs with the role "{role}"')
def function_role(context, name, role):
    assert c("lambda").get_function_configuration(FunctionName=name)["Role"].endswith(f":role/{role}")


@then('the function was invoked {n:d} times')
def invoked(context, n):
    assert len(context.invocations) == n, len(context.invocations)


@then('the invocations wrote {n:d} metric lines whose Errors add up to {e:d}')
def invocation_lines(context, n, e):
    lines = [json.loads(ln) for inv in context.invocations for ln in inv["lines"]]
    assert len(lines) == n and sum(ln["Errors"] for ln in lines) == e, lines


@then('the alarms, the dashboard, the function, the topic, the budget and the role all start with "{start}"')
def names_start(context, start):
    names = [a["AlarmName"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"]]
    names += [d["DashboardName"] for d in c("cloudwatch").list_dashboards()["DashboardEntries"]]
    names += [f["FunctionName"] for f in c("lambda").list_functions()["Functions"]]
    names += [t["TopicArn"].split(":")[-1] for t in c("sns").list_topics()["Topics"]]
    names += [b["BudgetName"] for b in c("budgets").describe_budgets(AccountId=account()).get("Budgets", [])]
    names += [r["RoleName"] for r in c("iam").list_roles()["Roles"]]
    assert len(names) == 10 and all(n.startswith(start) for n in names), names


@then('the function, the topic, the budget and the role carry the tag "{tag}"')
def all_tagged(context, tag):
    s = Settings()
    key, value = tag.split("=")
    fn = c("lambda").get_function(FunctionName=s.function_name)["Configuration"]["FunctionArn"]
    assert c("lambda").list_tags(Resource=fn)["Tags"].get(key) == value
    topic = [t["TopicArn"] for t in c("sns").list_topics()["Topics"]][0]
    assert {"Key": key, "Value": value} in c("sns").list_tags_for_resource(ResourceArn=topic)["Tags"]
    budget = f"arn:aws:budgets::{account()}:budget/{s.budget_name}"
    assert {"Key": key, "Value": value} in c("budgets").list_tags_for_resource(ResourceARN=budget)["ResourceTags"]
    assert {"Key": key, "Value": value} in c("iam").list_role_tags(RoleName=s.role_name)["Tags"]


@then('every Lambda, CloudWatch and SNS call went to "{region}"')
def regions(context, region):
    for op in ("CreateFunction", "PutFunctionConcurrency", "PutMetricAlarm", "PutDashboard", "CreateTopic",
               "Invoke"):
        assert context.calls.regions(op) == {region}, (op, context.calls.regions(op))


@then('every AWS Budgets call went to its one endpoint, "{region}"')
def budgets_region(context, region):
    for op in ("DescribeBudget", "CreateBudget"):
        assert context.calls.regions(op) == {region}, (op, context.calls.regions(op))


@then('the alarms of the account are "{names}"')
def alarms_are(context, names):
    got = sorted(a["AlarmName"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"])
    assert got == sorted(csv(names)), got


@then('every alarm notifies the topic "{topic}" on ALARM and on OK')
def alarm_actions(context, topic):
    for a in c("cloudwatch").describe_alarms()["MetricAlarms"]:
        assert [x.split(":")[-1] for x in a["AlarmActions"]] == [topic], a
        assert [x.split(":")[-1] for x in a["OKActions"]] == [topic], a


@then('every alarm treats missing data as "{how}"')
def missing(context, how):
    assert all(a["TreatMissingData"] == how for a in c("cloudwatch").describe_alarms()["MetricAlarms"])


@then('the alarms have these settings:')
def alarm_settings(context):
    alarms = {a["AlarmName"]: a for a in c("cloudwatch").describe_alarms()["MetricAlarms"]}
    for row in context.table:
        a = alarms[row["alarm"]]
        got = [a["MetricName"], a.get("ExtendedStatistic") or a.get("Statistic"), a["ComparisonOperator"],
               a["Threshold"], a["Period"], a["EvaluationPeriods"], a["DatapointsToAlarm"]]
        want = [row["metric"], row["statistic"], row["comparison"], float(row["threshold"]), int(row["period"]),
                int(row["periods"]), int(row["to alarm"])]
        assert got == want, (row["alarm"], got)
        if a["Namespace"] != "AWS/Lambda":
            assert a["Dimensions"] == [{"Name": "Service", "Value": "test-app"}] and a["Namespace"] == "test/app", a


@then('the alarms "{names}" watch the namespace "{ns}" with FunctionName "{fn}"')
def lambda_alarms(context, names, ns, fn):
    alarms = {a["AlarmName"]: a for a in c("cloudwatch").describe_alarms()["MetricAlarms"]}
    for name in csv(names):
        a = alarms[name]
        assert a["Namespace"] == ns and a["Dimensions"] == [{"Name": "FunctionName", "Value": fn}], a


@then('the dashboard "{name}" has the widgets "{titles}"')
def dashboard_widgets(context, name, titles):
    body = json.loads(c("cloudwatch").get_dashboard(DashboardName=name)["DashboardBody"])
    context.dashboard = body
    assert [w["properties"]["title"] for w in body["widgets"]] == [x.strip() for x in titles.split(";")], body


@then("the dashboard's alarm widget shows the three alarms")
def dashboard_alarms(context):
    arns = sorted(a["AlarmArn"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"])
    widget = [w for w in context.dashboard["widgets"] if w["type"] == "alarm"][0]
    assert sorted(widget["properties"]["alarms"]) == arns, widget


@then('each alarm was set to "{first}" and then to "{second}"')
def set_states(context, first, second):
    calls = [(p["AlarmName"], p["StateValue"]) for p in context.calls.of("SetAlarmState")]
    names = [Settings().alarm_name(x) for x in ("errors", "p95-latency", "daily-cost", "lambda-errors",
                                                 "lambda-throttles")]
    assert calls == [(n, s) for n in names for s in (first, second)], calls


@then('PutMetricData was called {n:d} times, {s:d} seconds apart')
def put_metric_data(context, n, s):
    assert len(context.calls.of("PutMetricData")) == n
    assert context.sleeps.count(s) >= n, context.sleeps


@then('each PutMetricData call sent Errors {e:d} and {k:d} LatencyMs values above {v:d} in "{ns}"')
def put_metric_values(context, e, k, v, ns):
    for p in context.calls.of("PutMetricData"):
        assert p["Namespace"] == ns
        data = {d["MetricName"]: d for d in p["MetricData"]}
        assert data["Errors"]["Value"] == e, data
        assert len(data["LatencyMs"]["Values"]) == k and min(data["LatencyMs"]["Values"]) > v, data


@then('the topic\'s policy lets "{services}" publish, each only for this account')
def topic_policy(context, services):
    arn = c("sns").list_topics()["Topics"][0]["TopicArn"]
    policy = json.loads(c("sns").get_topic_attributes(TopicArn=arn)["Attributes"]["Policy"])
    got = []
    for st in policy["Statement"]:
        assert st["Effect"] == "Allow" and st["Action"] == "SNS:Publish" and st["Resource"] == arn, st
        assert st["Condition"] == {"StringEquals": {"aws:SourceAccount": account()}}, st
        got.append(st["Principal"]["Service"])
    assert sorted(got) == csv(services), got


@then('the topic has {n:d} email subscriptions')
def email_subs(context, n):
    subs = c("sns").list_subscriptions()["Subscriptions"]
    assert sum(1 for x in subs if x["Protocol"] == "email") == n, subs


@then('the account has no alarm, dashboard, function, log group, budget, topic or role')
def nothing_left(context):
    assert c("cloudwatch").describe_alarms()["MetricAlarms"] == []
    assert c("cloudwatch").list_dashboards()["DashboardEntries"] == []
    assert c("lambda").list_functions()["Functions"] == []
    assert c("logs").describe_log_groups()["logGroups"] == []
    assert c("budgets").describe_budgets(AccountId=account()).get("Budgets", []) == []
    assert c("sns").list_topics()["Topics"] == []
    assert c("iam").list_roles()["Roles"] == []


@then('no Delete call was made')
def no_delete(context):
    deletes = [op for op, _, _ in context.calls.log if op.startswith("Delete")]
    assert deletes == [], deletes


@then('the function "{name}" and its log group are still there')
def function_kept(context, name):
    c("lambda").get_function(FunctionName=name)
    assert c("logs").describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{name}")["logGroups"]


@then('the role "{name}" is still there')
def role_kept(context, name):
    c("iam").get_role(RoleName=name)


def topic_arn(name):
    return [x["TopicArn"] for x in c("sns").list_topics()["Topics"] if x["TopicArn"].split(":")[-1] == name][0]


@given('the topic "{name}" has lost the tag "{key}"')
def topic_untagged(context, name, key):
    c("sns").untag_resource(ResourceArn=topic_arn(name), TagKeys=[key])


@then('the topic "{name}" is still there')
def topic_kept(context, name):
    assert topic_arn(name)


@then('the stop-reason widget has one line for each of "{reasons}"')
def stop_reason_widget(context, reasons):
    widget = [w for w in context.dashboard["widgets"] if w["properties"]["title"].startswith("Loop tasks")][0]
    lines = widget["properties"]["metrics"]
    assert [m[5] for m in lines] == csv(reasons), lines
    assert all(m[1] == "AgentSteps" and m[6]["stat"] == "SampleCount" for m in lines), lines
