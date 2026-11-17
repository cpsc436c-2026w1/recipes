"""The bash scenarios under DRY=1: the commands they would run, checked against README.md's decisions."""

import json
import re

from conftest import commands


def policy_of(stdout):
    """The policy document main/run.sh prints in step 3 (it spans several lines)."""
    text = stdout.split("aws iam put-role-policy", 1)[1].split("--policy-document ", 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


def test_role_is_least_privilege(dry):
    pol = policy_of(dry("main/run.sh"))
    tables, logs = pol["Statement"]
    # Decision (role): three DynamoDB actions, no wildcard, on the three named tables only.
    assert tables["Action"] == ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"]
    assert [r.rsplit("/", 1)[1] for r in tables["Resource"]] == ["recipe-trips", "recipe-rider-trips", "recipe-drivers"]
    assert logs["Resource"].endswith(":log-group:/aws/lambda/recipe-api:*")
    assert not any("*" in a for s in pol["Statement"] for a in s["Action"])


def test_function_and_url(dry):
    cmds = commands(dry("main/run.sh"))
    create = next(c for c in cmds if c.startswith("aws lambda create-function "))
    assert "--architectures arm64" in create and "--memory-size 128" in create and "Variables={PREFIX=recipe}" in create
    # A public URL needs both statements; with only the first, callers get 403.
    perms = [c for c in cmds if c.startswith("aws lambda add-permission")]
    assert ["lambda:InvokeFunctionUrl" in perms[0], "lambda:InvokeFunction " in perms[1]] == [True, True]


def test_clean_up_removes_the_url_first(dry):
    cmds = commands(dry("main/run.sh"))
    tail = [c.split()[2] for c in cmds[-5:]]
    assert tail == ["delete-function-url-config", "delete-function", "delete-log-group", "delete-role-policy",
                    "delete-role"]
    assert "delete-function-url-config" not in dry("main/run.sh", KEEP="1")


def test_prefix_names_everything(dry):
    out = dry("main/run.sh", PREFIX="recipe-ana") + dry("clean_up.sh", PREFIX="recipe-ana")
    names = set(re.findall(r"--(?:function|role)-name (\S+)", out))
    assert names == {"recipe-ana-api", "recipe-ana-api-role"}


def test_existing_role_is_used_and_kept(dry):
    arn = "arn:aws:iam::111122223333:role/team-lambda"
    cmds = commands(dry("main/run.sh", ROLE_ARN=arn) + dry("clean_up.sh", ROLE_ARN=arn))
    assert not any(c.startswith(("aws iam create-role", "aws iam put-role-policy", "aws iam delete-role"))
                   for c in cmds)
    create = next(c for c in cmds if c.startswith("aws lambda create-function "))
    assert f"--role {arn}" in create


def test_extensions(dry):
    redeploy = commands(dry("ext-redeploy/run.sh"))
    assert any(c.startswith("aws lambda create-function ") for c in redeploy)  # no function under DRY: create path
    assert any(c.startswith("aws lambda invoke") for c in redeploy)
    metrics = commands(dry("ext-metrics/run.sh"))
    assert [c.split("--metric-name ")[1].split()[0] for c in metrics if "get-metric-statistics" in c] == \
        ["Invocations", "Duration"]
