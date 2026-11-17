"""The clean-up, offline: after a deploy on the stand-ins, everything the deploy created is gone."""
import json

import pytest

import clean_up
from main.config import load_settings
from main.infra import deploy
from tests.conftest import no_wait
from tests.fakes import ACCOUNT


@pytest.fixture
def deployed(aws, monkeypatch, tmp_path):
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    monkeypatch.setenv("LOGFIRE_TOKEN", "made-up-logfire-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://llm.test.invalid/v1")
    monkeypatch.setattr("main.infra.BUILD", tmp_path)
    s = load_settings()
    deploy(s, aws, log=lambda line: None, wait=no_wait, with_dependencies=False)
    runtime_id = next(iter(aws.agentcore.runtimes))
    aws.logs.create_log_group(logGroupName=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT")   # the Runtime makes it
    return s, aws


def left(s, aws) -> dict:
    """Everything of this prefix that is still there."""
    return {
        "tables": aws.dynamodb.list_tables()["TableNames"],
        "functions": [f["FunctionName"] for f in aws.lambda_.list_functions()["Functions"]],
        "log groups": [g["logGroupName"] for g in aws.logs.describe_log_groups()["logGroups"]],
        "parameters": [p["Name"] for p in aws.ssm.describe_parameters()["Parameters"]],
        "buckets": [b["Name"] for b in aws.s3.list_buckets()["Buckets"]],
        "roles": [r["RoleName"] for r in aws.iam.list_roles()["Roles"]],
        "gateways": list(aws.agentcore.gateways), "targets": list(aws.agentcore.targets),
        "runtimes": list(aws.agentcore.runtimes),
    }


def test_clean_up_removes_everything_the_deploy_created(deployed):
    s, aws = deployed
    before = left(s, aws)
    assert all(before.values()) and len(before["log groups"]) == 2 and len(before["parameters"]) == 2
    steps = clean_up.clean_up(s, aws, wait=no_wait)
    assert steps.failed == [] and steps.report(log=lambda line: None) == 0
    assert left(s, aws) == {kind: [] for kind in before}
    removed = "\n".join(steps.removed)
    for what in (f"agentcore runtime {s.runtime_name}", f"agentcore gateway {s.gateway_name}", "gateway target",
                 f"lambda function {s.tools_function}", f"parameter {s.token_param}", f"parameter {s.logfire_param}",
                 f"s3 bucket {s.code_bucket(ACCOUNT)}", f"dynamodb table {s.books_table}",
                 f"dynamodb table {s.holds_table}", f"role {s.role_name('runtime')}"):
        assert what in removed, what


def test_the_target_goes_before_the_gateway(deployed):
    s, aws = deployed
    clean_up.clean_up(s, aws, wait=no_wait)
    order = [name for name, _ in aws.agentcore.calls if name.startswith("Delete")]
    assert order == ["DeleteAgentRuntime", "DeleteGatewayTarget", "DeleteGateway"]


def test_a_dry_run_lists_and_deletes_nothing(deployed):
    s, aws = deployed
    before = left(s, aws)
    steps = clean_up.clean_up(s, aws, dry_run=True, wait=no_wait)
    assert left(s, aws) == before and len(steps.removed) >= 14 and steps.failed == []
    lines = []
    steps.report(log=lines.append)
    assert all(line.startswith("would remove") for line in lines)


def test_a_second_clean_up_reports_everything_as_absent(deployed):
    s, aws = deployed
    clean_up.clean_up(s, aws, wait=no_wait)
    again = clean_up.clean_up(s, aws, wait=no_wait)
    assert again.removed == [] and again.failed == [] and len(again.absent) >= 10


def test_a_refused_delete_does_not_stop_the_rest_and_the_exit_code_is_1(deployed):
    s, aws = deployed
    aws.agentcore.refuse.add("DeleteAgentRuntime")
    steps = clean_up.clean_up(s, aws, wait=no_wait)
    assert len(steps.failed) == 1 and "AccessDeniedException" in steps.failed[0]
    assert steps.report(log=lambda line: None) == 1
    after = left(s, aws)
    assert len(after["runtimes"]) == 1                                  # the one that was refused is still there
    assert all(not v for k, v in after.items() if k not in ("runtimes", "log groups"))


def test_a_role_you_named_is_left_alone(aws, monkeypatch, tmp_path):
    document = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]})
    allow = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": "*"}]})
    arn = aws.iam.create_role(RoleName="my-own-lambda-role", AssumeRolePolicyDocument=document)["Role"]["Arn"]
    aws.iam.put_role_policy(RoleName="my-own-lambda-role", PolicyName="mine", PolicyDocument=allow)
    monkeypatch.setenv("LAMBDA_ROLE_ARN", arn)
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://llm.test.invalid/v1")
    monkeypatch.setattr("main.infra.BUILD", tmp_path)
    s = load_settings()
    deploy(s, aws, log=lambda line: None, wait=no_wait, with_dependencies=False)
    steps = clean_up.clean_up(s, aws, wait=no_wait)
    assert steps.failed == []
    assert [r["RoleName"] for r in aws.iam.list_roles()["Roles"]] == ["my-own-lambda-role"]
    assert aws.iam.list_role_policies(RoleName="my-own-lambda-role")["PolicyNames"] == ["mine"]


def test_the_command_prints_one_line_per_resource_and_a_total(deployed, monkeypatch, capsys):
    s, aws = deployed
    monkeypatch.setattr("clean_up.Clients.for_region", lambda region: aws)
    monkeypatch.setattr("clean_up.time.sleep", no_wait)
    with pytest.raises(SystemExit) as stop:
        clean_up.main([])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and f"removed  dynamodb table {s.books_table}" in out
    assert "clean-up for PREFIX=test in ca-central-1" in out and "0 failed" in out
