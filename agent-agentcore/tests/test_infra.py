"""The deploy, offline: moto stands in for DynamoDB, IAM, Lambda, S3, Parameter Store and CloudWatch Logs, and
tests/fakes.py for AgentCore, where every call is checked against the service model that ships with botocore."""
import importlib.util
import io
import json
import sys
import zipfile

import pytest

from main.config import TARGET_NAME, load_settings
from main.contracts import TOOLS, tool_schema
from main.infra import CODE_KEY, deploy, role_policies, tools_zip
from tests.conftest import no_wait
from tests.fakes import ACCOUNT, lambda_context


@pytest.fixture
def deployed(aws, monkeypatch, tmp_path):
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://llm.test.invalid/v1")
    monkeypatch.setattr("main.infra.BUILD", tmp_path)
    s = load_settings()
    lines = []
    out = deploy(s, aws, log=lines.append, wait=no_wait, with_dependencies=False)
    return s, aws, out, lines


def calls(aws, operation: str) -> list[dict]:
    return [params for name, params in aws.agentcore.calls if name == operation]


def test_the_deploy_creates_the_function_the_gateway_and_one_target_with_the_tools(deployed):
    s, aws, out, _ = deployed
    function = aws.lambda_.get_function(FunctionName=s.tools_function)["Configuration"]
    assert function["Environment"]["Variables"] == {"BOOKS_TABLE": s.books_table, "HOLDS_TABLE": s.holds_table}
    gateway = calls(aws, "CreateGateway")[0]
    assert (gateway["name"], gateway["protocolType"], gateway["authorizerType"]) == (s.gateway_name, "MCP", "AWS_IAM")
    target = calls(aws, "CreateGatewayTarget")[0]
    assert target["name"] == TARGET_NAME and target["credentialProviderConfigurations"] == [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    lam = target["targetConfiguration"]["mcp"]["lambda"]
    assert lam["lambdaArn"] == out["function_arn"]
    assert lam["toolSchema"]["inlinePayload"] == tool_schema()
    assert [t["name"] for t in lam["toolSchema"]["inlinePayload"]] == list(TOOLS)


def test_the_deploy_loads_the_example_data(deployed):
    s, aws, _, lines = deployed
    assert aws.dynamodb.scan(TableName=s.books_table, Select="COUNT")["Count"] == 12
    assert "12 books" in lines[0]


def test_the_runtime_comes_from_a_zip_in_s3_and_its_environment_holds_no_secret(deployed):
    s, aws, out, _ = deployed
    runtime = calls(aws, "CreateAgentRuntime")[0]
    code = runtime["agentRuntimeArtifact"]["codeConfiguration"]
    assert code == {"code": {"s3": {"bucket": s.code_bucket(ACCOUNT), "prefix": CODE_KEY}},
                    "runtime": "PYTHON_3_12", "entryPoint": ["agent_entry.py"]}
    assert runtime["agentRuntimeName"] == "test_agent"
    assert runtime["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 300, "maxLifetime": 1800}
    env = runtime["environmentVariables"]
    assert "made-up-course-token" not in json.dumps(runtime)
    assert env["KB_MCP_TOKEN_PARAM"] == s.token_param and env["TOOLS_GATEWAY_URL"] == out["gateway"]["url"]
    assert "KB_MCP_TOKEN" not in env and "LOGFIRE_TOKEN" not in env
    names = zipfile.ZipFile(io.BytesIO(aws.s3.get_object(Bucket=s.code_bucket(ACCOUNT), Key=CODE_KEY)["Body"].read())).namelist()
    assert {"agent_entry.py", "main/__init__.py", "main/agent.py", "main/hooks.py", "main/config.py", "data/books.jsonl"} <= set(names)
    assert "main.py" not in names                      # a main.py beside the package main/ would shadow it
    assert not [n for n in names if n.endswith((".env", "infra.py")) or "tests/" in n]


def test_the_course_token_is_a_securestring_parameter(deployed):
    s, aws, _, lines = deployed
    described = aws.ssm.describe_parameters()["Parameters"]
    assert [(p["Name"], p["Type"]) for p in described] == [(s.token_param, "SecureString")]
    assert aws.ssm.get_parameter(Name=s.token_param, WithDecryption=True)["Parameter"]["Value"] == "made-up-course-token"
    assert "made-up-course-token" not in "\n".join(lines)


def test_a_second_deploy_updates_and_creates_nothing_twice(deployed):
    s, aws, first, _ = deployed
    again = deploy(s, aws, log=lambda line: None, wait=no_wait, with_dependencies=False)
    assert again["gateway"]["id"] == first["gateway"]["id"] and again["runtime"]["id"] == first["runtime"]["id"]
    assert len(aws.agentcore.gateways) == len(aws.agentcore.targets) == len(aws.agentcore.runtimes) == 1
    assert len(calls(aws, "CreateGateway")) == len(calls(aws, "CreateAgentRuntime")) == 1
    assert len(calls(aws, "UpdateGatewayTarget")) == len(calls(aws, "UpdateAgentRuntime")) == 1


def test_the_created_roles_allow_only_what_the_recipe_does(deployed):
    s, aws, out, _ = deployed
    for part in ("lambda", "gateway", "runtime"):
        policy = aws.iam.get_role_policy(RoleName=s.role_name(part), PolicyName="agent-agentcore")["PolicyDocument"]
        text = json.dumps(policy)
        assert '"Action": "*"' not in text and ":*\"" not in text.replace("log-group:*", "").replace(":log-stream:*", "").replace(f"{s.tools_function}:*", "")
        assert "bedrock-agentcore:*" not in text and "iam:" not in text
    runtime = json.dumps(aws.iam.get_role_policy(RoleName=s.role_name("runtime"), PolicyName="agent-agentcore")["PolicyDocument"])
    assert out["gateway"]["arn"] in runtime and s.token_param in runtime     # this gateway, this parameter
    trust = aws.iam.get_role(RoleName=s.role_name("runtime"))["Role"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}


def test_a_role_you_name_is_used_and_no_role_is_created_for_it(aws, monkeypatch, tmp_path):
    mine = "arn:aws:iam::123456789012:role/my-own-gateway-role"
    monkeypatch.setenv("GATEWAY_ROLE_ARN", mine)
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://llm.test.invalid/v1")
    monkeypatch.setattr("main.infra.BUILD", tmp_path)
    s = load_settings()
    deploy(s, aws, log=lambda line: None, wait=no_wait, with_dependencies=False)
    assert calls(aws, "CreateGateway")[0]["roleArn"] == mine
    roles = [r["RoleName"] for r in aws.iam.list_roles()["Roles"]]
    assert sorted(roles) == sorted([s.role_name("lambda"), s.role_name("runtime")])


@pytest.mark.parametrize("empty", ["KB_MCP_TOKEN", "COURSE_GATEWAY_URL"])
def test_the_deploy_asks_for_the_token_and_the_address_before_its_first_aws_call(aws, monkeypatch, empty):
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://llm.test.invalid/v1")
    monkeypatch.setenv(empty, "")
    with pytest.raises(SystemExit) as stop:
        deploy(load_settings(), aws, log=lambda line: None, wait=no_wait, with_dependencies=False)
    assert f"Set {empty} in .env" in str(stop.value)
    assert aws.agentcore.calls == [] and aws.dynamodb.list_tables()["TableNames"] == []


def test_role_policies_name_this_prefixs_resources_only(s):
    for part, (_, policy) in role_policies(s, ACCOUNT).items():
        for statement in policy["Statement"]:
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            for r in resources:
                assert r == "*" or "test-agent" in r or "/aws/bedrock-agentcore/runtimes/" in r or r.endswith("log-group:*"), (part, r)


# ---- the tools function, as the Gateway calls it -----------------------------------------------------------------
@pytest.fixture
def handler(deployed, tmp_path, monkeypatch):
    """handler.py imported from the unpacked zip, the way Lambda has it: the zip's files and nothing else of ours."""
    s, aws, _, _ = deployed
    folder = tmp_path / "function"
    zipfile.ZipFile(io.BytesIO(tools_zip())).extractall(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["handler.py", "store.py", "tools.py"]
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setenv("BOOKS_TABLE", s.books_table)
    monkeypatch.setenv("HOLDS_TABLE", s.holds_table)
    for name in ("handler", "store", "tools"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("handler", folder / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in ("handler", "store", "tools"):
        sys.modules.pop(name, None)


def test_the_function_reads_the_tools_name_from_the_client_context(handler):
    found = handler.lambda_handler({"query": "austen"}, lambda_context(f"{TARGET_NAME}___search_books"))
    assert [b["book_id"] for b in found["books"]] == ["b02", "b03"]
    placed = handler.lambda_handler({"member": "sam", "book_id": "b02", "request_id": "k1"},
                                    lambda_context(f"{TARGET_NAME}___place_hold"))
    again = handler.lambda_handler({"member": "sam", "book_id": "b02", "request_id": "k1"},
                                   lambda_context(f"{TARGET_NAME}___place_hold"))
    assert placed["placed"] is True and again["duplicate_request"] is True
    holds = handler.lambda_handler({"member": "sam"}, lambda_context(f"{TARGET_NAME}___get_holds"))
    assert holds == {"member": "sam", "holds": [{"book_id": "b02", "title": "Emma"}]}


def test_the_function_answers_a_tool_it_does_not_have_with_an_error_result(handler):
    assert "unknown tool" in handler.lambda_handler({}, lambda_context(f"{TARGET_NAME}___drop_table"))["error"]
    assert "wrong arguments" in handler.lambda_handler({"id": "b01"}, lambda_context(f"{TARGET_NAME}___get_book"))["error"]


def test_the_functions_zip_imports_nothing_outside_the_standard_library_and_boto3():
    import ast

    allowed = {"os", "re", "json", "time", "decimal", "pathlib", "__future__", "boto3", "botocore", "store", "tools"}
    for name in ("handler.py", "store.py", "tools.py"):
        tree = ast.parse(zipfile.ZipFile(io.BytesIO(tools_zip())).read(name))
        imported = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert imported <= allowed, (name, imported - allowed)
