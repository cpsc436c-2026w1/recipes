"""Run scripts end to end against moto; clean_up.py removes everything set_up created."""

from __future__ import annotations

import io

import clean_up
import loop_run
import run


def test_offline_table_is_diagonal(s, deployed):
    lines = run.check_offline(s, deployed["topic"])
    rows = {ln.split()[0]: ln.split()[1:] for ln in lines[1:]}
    assert rows == {"normal": ["OK", "OK", "OK"], "errors": ["ALARM", "OK", "OK"],
                    "slow": ["OK", "ALARM", "OK"], "expensive": ["OK", "OK", "ALARM"]}


def test_loop_run_stops_every_runaway():
    lines = loop_run.main(out=io.StringIO())
    reasons = [ln.split()[-4] for ln in lines[1:]]
    assert reasons == ["done", "step_cap", "deadline", "cost_cap", "budget_exhausted"]


def inventory(c: dict) -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    acct = c["sts"].get_caller_identity()["Account"]
    return {
        "alarms": sorted(a["AlarmName"] for a in c["cw"].describe_alarms()["MetricAlarms"]),
        "dashboards": sorted(d["DashboardName"] for d in c["cw"].list_dashboards()["DashboardEntries"]),
        "functions": sorted(f["FunctionName"] for f in c["lam"].list_functions()["Functions"]),
        "log groups": sorted(g["logGroupName"] for g in c["logs"].describe_log_groups()["logGroups"]),
        "budgets": sorted(x["BudgetName"] for x in c["budgets"].describe_budgets(AccountId=acct).get("Budgets", [])),
        "topics": sorted(x["TopicArn"] for x in c["sns"].list_topics()["Topics"]),
        "subscriptions": sorted(x["Endpoint"] for x in c["sns"].list_subscriptions()["Subscriptions"]),
        "roles": sorted(r["RoleName"] for r in c["iam"].list_roles()["Roles"]),
    }


def set_up_like_a_real_run(s, c):
    run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    c["logs"].create_log_group(logGroupName=f"/aws/lambda/{s.function_name}")  # Lambda makes it on first run


def test_clean_up_removes_everything():
    from config import Settings

    s = Settings(email="ana@example.com")  # with ALARM_EMAIL: the run also creates a subscription
    c = clean_up.clients(s)
    before = inventory(c)
    assert not any(before.values())
    set_up_like_a_real_run(s, c)
    during = inventory(c)
    assert all(during.values()), during  # one or more of every kind, the subscription included
    planned = clean_up.clean_up(s, c, dry_run=True)
    assert len(planned.removed) == 5 + 6  # five alarms, dashboard, function, log group, budget, topic, role
    assert inventory(c) == during  # the dry run deleted nothing
    done = clean_up.clean_up(s, c)
    assert len(done.removed) == 11 and not done.failed
    assert inventory(c) == before
    again = clean_up.clean_up(s, c)  # a second clean-up finds nothing and fails nothing
    assert again.removed == [] and not again.failed and len(again.absent) == 11


def test_second_run_and_run_right_after_clean_up(s):
    c = clean_up.clients(s)
    first, _, _ = run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    second, _, _ = run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    assert any("created" in x for x in first)
    assert not any("created" in x for x in second) and any("updated" in x for x in second)
    assert not clean_up.clean_up(s, c).failed
    third, _, _ = run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    assert [x.split(":")[0] for x in third] == [x.split(":")[0] for x in first]  # the same lines as a first run
    assert len(clean_up.clean_up(s, c, dry_run=True).removed) == 10  # all back (no log group: nothing ran)


def test_clean_up_goes_on_after_a_step_is_refused(s, monkeypatch, capsys):
    """A sandbox that refuses AWS Budgets: the topic and the role after it are still removed, and the exit is 1."""
    import pytest
    from botocore.exceptions import ClientError

    c = clean_up.clients(s)
    set_up_like_a_real_run(s, c)

    def refuse(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}}, "DescribeBudget")

    monkeypatch.setattr(c["budgets"], "describe_budget", refuse)
    monkeypatch.setattr(clean_up, "clients", lambda s: c)
    monkeypatch.setattr("sys.argv", ["clean_up.py"])
    with pytest.raises(SystemExit) as e:
        clean_up.main()
    out = capsys.readouterr()
    assert e.value.code == 1 and "FAILED budget test-monthly: AccessDeniedException" in out.err
    assert "removed sns topic test-limits-alarms" in out.out and "removed iam role test-app-role" in out.out
    left = inventory(c)
    assert left["budgets"] == ["test-monthly"]
    assert not any(v for k, v in left.items() if k != "budgets")


def test_recipe_role_has_logs_policy_only(s, deployed):
    iam = deployed["c"]["iam"]
    assert iam.list_attached_role_policies(RoleName=s.role_name)["AttachedPolicies"] == []
    names = iam.list_role_policies(RoleName=s.role_name)["PolicyNames"]
    assert names == ["write-own-logs"]
    doc = iam.get_role_policy(RoleName=s.role_name, PolicyName=names[0])["PolicyDocument"]
    actions = {a for st in doc["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])}
    assert actions == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}
    assert all(f"/aws/lambda/{s.function_name}" in st["Resource"] for st in doc["Statement"])
    assert deployed["c"]["lam"].get_function_configuration(FunctionName=s.function_name)["Role"].endswith(s.role_name)


def test_existing_role_is_used_and_left_alone(s):
    import json

    import boto3

    c = run.clients(s)
    c["logs"] = boto3.client("logs", region_name=s.region)
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "lambda.amazonaws.com"}}]}
    mine = c["iam"].create_role(RoleName="team-lambda", AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    lines, _, _ = run.set_up(s, c, mine, run.DEFAULT_ACCOUNT, wait_s=0)
    assert "role from LAMBDA_ROLE_ARN: team-lambda" in lines
    clean_up.clean_up(s, c)
    assert c["iam"].get_role(RoleName="team-lambda")["Role"]["Arn"] == mine
    assert not any(r["RoleName"] == s.role_name for r in c["iam"].list_roles()["Roles"])


def _someone_elses(s, c, *, function=False, budget=False):
    """A function or a budget with the recipe's name and without its tag, made before any run."""
    import json

    acct = c["sts"].get_caller_identity()["Account"]
    if function:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                         "Principal": {"Service": "lambda.amazonaws.com"}}]}
        role = c["iam"].create_role(RoleName="team-fn", AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        from limits import app_zip
        c["lam"].create_function(FunctionName=s.function_name, Runtime="python3.13", Role=role,
                                 Handler="app.handler", Code={"ZipFile": app_zip()})
        c["logs"].create_log_group(logGroupName=f"/aws/lambda/{s.function_name}")
    if budget:
        c["budgets"].create_budget(AccountId=acct, Budget={
            "BudgetName": s.budget_name, "BudgetLimit": {"Amount": "99.00", "Unit": "USD"},
            "TimeUnit": "MONTHLY", "BudgetType": "COST"})


def test_an_untagged_function_with_the_name_is_not_updated_or_deleted(s):
    import pytest

    c = clean_up.clients(s)
    _someone_elses(s, c, function=True)
    code = c["lam"].get_function(FunctionName=s.function_name)["Configuration"]["CodeSha256"]
    with pytest.raises(SystemExit) as e:
        run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    assert "does not carry the tag recipe=limits-and-alarms" in str(e.value) and s.function_name in str(e.value)
    assert c["lam"].get_function(FunctionName=s.function_name)["Configuration"]["CodeSha256"] == code
    dry = clean_up.clean_up(s, c, dry_run=True)
    done = clean_up.clean_up(s, c)
    assert done.removed == dry.removed and done.failed == dry.failed
    assert [f.split(":")[0] for f in done.failed] == [f"lambda function {s.function_name}",
                                                      f"log group /aws/lambda/{s.function_name}"]
    assert "does not carry the tag" in done.failed[0]
    assert c["lam"].get_function(FunctionName=s.function_name)
    assert c["logs"].describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{s.function_name}")["logGroups"]


def test_an_untagged_budget_with_the_name_is_not_adopted_or_deleted(s, capsys, monkeypatch):
    import pytest

    c = clean_up.clients(s)
    _someone_elses(s, c, budget=True)
    with pytest.raises(SystemExit) as e:
        run.set_up(s, c, "", run.DEFAULT_ACCOUNT, wait_s=0)
    assert f"budget {s.budget_name} exists but does not carry the tag" in str(e.value)
    acct = c["sts"].get_caller_identity()["Account"]
    monkeypatch.setattr(clean_up, "clients", lambda s: c)
    monkeypatch.setattr("sys.argv", ["clean_up.py"])
    with pytest.raises(SystemExit) as e:
        clean_up.main()
    assert e.value.code == 1 and f"FAILED budget {s.budget_name}: refused" in capsys.readouterr().err
    kept = c["budgets"].describe_budget(AccountId=acct, BudgetName=s.budget_name)["Budget"]
    assert kept["BudgetLimit"]["Amount"].startswith("99")


def test_the_recipe_budget_and_function_carry_the_tag(s, deployed, _budget_tags):
    from limits import budget_is_ours, function_is_ours

    c = deployed["c"]
    acct = c["sts"].get_caller_identity()["Account"]
    assert function_is_ours(c["lam"], s.function_name) and budget_is_ours(c["budgets"], acct, s.budget_name)
