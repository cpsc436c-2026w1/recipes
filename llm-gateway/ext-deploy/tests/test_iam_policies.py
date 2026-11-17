"""The policy files in infra/iam/ against the calls the code makes and the resources the template names.

The README says the deploy policy holds the permissions for `gw-admin`, and that the function's role lets the
function read and update items, read one secret, publish to one topic and write its own log. These tests record
the AWS operations the code sends (to moto) and compare them with the policy files.
"""

import contextlib
import fnmatch
import json
import re
import time
from pathlib import Path

from conftest import bearer, chat_body, run_admin, wait_for_usage
from gateway import aws

INFRA = Path(__file__).resolve().parents[1] / "infra"
PREFIX = {"dynamodb": "dynamodb", "sns": "sns", "secretsmanager": "secretsmanager", "lambda": "lambda"}


def allowed(policy_file: str, sids: set[str] | None = None) -> set[str]:
    doc = json.loads((INFRA / "iam" / policy_file).read_text())
    actions: set[str] = set()
    for st in doc["Statement"]:
        if st["Effect"] != "Allow" or (sids is not None and st.get("Sid") not in sids):
            continue
        acts = st["Action"]
        actions |= set([acts] if isinstance(acts, str) else acts)
    return actions


def _listed(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def not_allowed(calls: set[tuple[str, str]], policy_file: str, sids: set[str] | None = None) -> set:
    """The (action, resource ARN) pairs that no Allow statement of the policy (or of `sids`) covers."""
    doc = json.loads((INFRA / "iam" / policy_file).read_text())
    sts = [st for st in doc["Statement"] if st["Effect"] == "Allow" and (sids is None or st.get("Sid") in sids)]

    def covered(action, arn):
        return any(any(fnmatch.fnmatchcase(action, a) for a in _listed(st["Action"]))
                   and any(fnmatch.fnmatchcase(arn, r) for r in _listed(st["Resource"])) for st in sts)

    return {c for c in calls if not covered(*c)}


def _resource(service: str, region: str, params: dict) -> str:
    """The ARN IAM checks for one call, from its parameters (the account is moto's, as in the policy files)."""
    account = "111122223333"
    if service == "dynamodb":
        arn = f"arn:aws:dynamodb:{region}:{account}:table/{params['TableName']}"
        return arn + (f"/index/{params['IndexName']}" if params.get("IndexName") else "")
    if service == "sns":
        if "TopicArn" in params:
            return params["TopicArn"]
        return params["SubscriptionArn"].rsplit(":", 1)[0]   # Unsubscribe: the subscription's topic
    if service == "secretsmanager":
        sid = params["SecretId"]
        # A secret's ARN is its name plus a hyphen and six random characters.
        return sid if sid.startswith("arn:") else f"arn:aws:secretsmanager:{region}:{account}:secret:{sid}-AbCdEf"
    raise AssertionError(f"no resource rule for {service}")


@contextlib.contextmanager
def recorded():
    """The operations sent through the gateway's own boto3 handles, as IAM action names.

    The set holds the action names; `.calls` holds each (action, resource ARN) pair.
    """
    seen: set[str] = set()
    calls: set[tuple[str, str]] = set()
    clients = [aws.keys_table().meta.client, aws.usage_table().meta.client, aws.texts_table().meta.client,
               aws.sns(), aws.secretsmanager()]
    handlers = []

    for c in clients:
        def note(params, model, _region=c.meta.region_name, **_):
            service = model.service_model.service_name
            action = f"{PREFIX[service]}:{model.name}"
            seen.add(action)
            calls.add((action, _resource(service, _region, params)))

        handlers.append(note)
        c.meta.events.register("before-parameter-build", note)
    try:
        yield seen, calls
    finally:
        for c, note in zip(clients, handlers):
            c.meta.events.unregister("before-parameter-build", note)


def test_gw_admin_sends_only_what_the_deploy_policy_allows(issue, tmp_path):
    time.sleep(0.6)  # let a settle from an earlier test finish, so only this test's calls are recorded
    with recorded() as (seen, calls):
        key_id, _ = issue(owner="pol", team="t77")
        run_admin("list", "--owner", "pol")
        run_admin("spend", "--by", "key", "--key", key_id)
        run_admin("spend", "--reconcile")
        run_admin("budget", "set", key_id, "2")
        run_admin("rate", "set", key_id, "40")
        dump = tmp_path / "dump.json"
        run_admin("dump", str(dump))
        run_admin("load", str(dump), "--overwrite")
        run_admin("texts", str(tmp_path / "texts.jsonl"), "--key", key_id)
        run_admin("notify", "--owner", "pol", "--email", "pol@example.com")
        run_admin("revoke", key_id)
    assert {"dynamodb:PutItem", "dynamodb:Scan", "dynamodb:Query", "sns:Subscribe"} <= seen   # the recorder works
    policy = allowed("deploy-admin-policy.json", {"AdminKeysAndUsage", "AdminSubscriptions"})
    assert seen <= policy, f"gw-admin sends {sorted(seen - policy)}, which the deploy policy does not allow"
    # Each call is also on a resource the statement names (a table, its index, the topic).
    missing = not_allowed(calls, "deploy-admin-policy.json", {"AdminKeysAndUsage", "AdminSubscriptions"})
    assert not missing, f"gw-admin calls {sorted(missing)}, outside the deploy policy's resources"
    assert any(":table/llm-gateway-keys/index/by_key_id" in r for _, r in calls)   # the index is recorded


def test_the_function_sends_only_what_its_role_allows(issue, client):
    key_id, key = issue(owner="fn-role")
    time.sleep(0.6)
    with recorded() as (seen, calls):
        r = client.post("/v1/chat/completions", headers=bearer(key), json=chat_body(max_tokens=20))
        assert r.status_code == 200
        with client.stream("POST", "/v1/chat/completions", headers=bearer(key),
                           json=chat_body(max_tokens=20, stream=True)) as s:
            assert s.status_code == 200
            list(s.iter_bytes())
        assert client.get("/v1/models", headers=bearer(key)).status_code == 200
        time.sleep(1.0)  # the stream settles after its last byte; the test's own read of the usage row comes later
    assert len(wait_for_usage(key_id)) == 1 and wait_for_usage(key_id)[0]["requests"] == 2
    assert {"dynamodb:GetItem", "dynamodb:UpdateItem"} <= seen   # the recorder works
    policy = allowed("function-role-policy.json")
    assert seen <= policy, f"the function sends {sorted(seen - policy)}, which its role does not allow"
    missing = not_allowed(calls, "function-role-policy.json")
    assert not missing, f"the function calls {sorted(missing)}, outside its role's resources"


def test_a_call_on_another_resource_is_caught():
    # The resource check itself: the same action on a table the policy does not name is not allowed.
    other = {("dynamodb:GetItem", "arn:aws:dynamodb:ca-central-1:111122223333:table/someone-elses-table"),
             ("sns:Publish", "arn:aws:sns:ca-central-1:111122223333:another-topic")}
    assert not_allowed(other, "function-role-policy.json") == other
    ok = {("dynamodb:GetItem", "arn:aws:dynamodb:ca-central-1:111122223333:table/llm-gateway-keys")}
    assert not not_allowed(ok, "function-role-policy.json")


def test_policies_name_the_resources_the_template_creates():
    template = (INFRA / "template.yaml").read_text()
    tables = set(re.findall(r"TableName: (\S+)", template))
    topic = re.search(r"TopicName: (\S+)", template).group(1)
    function = re.search(r"FunctionName: (\S+)", template).group(1)
    assert tables == {"llm-gateway-keys", "llm-gateway-usage", "llm-gateway-texts"}
    for name in ("function-role-policy.json", "deploy-admin-policy.json"):
        text = (INFRA / "iam" / name).read_text()
        for t in tables:
            assert f":table/{t}\"" in text, (name, t)
        assert f":{topic}\"" in text
    fn = json.loads((INFRA / "iam" / "function-role-policy.json").read_text())
    # Least privilege: no wildcard action, and no resource wider than one named table, secret prefix, topic or log group.
    for st in fn["Statement"]:
        acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
        assert not [a for a in acts if "*" in a], st["Sid"]
        res = [st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]
        assert all(r != "*" and "llm-gateway" in r for r in res), st["Sid"]
    assert allowed("function-role-policy.json") == {
        "dynamodb:GetItem", "dynamodb:UpdateItem", "secretsmanager:GetSecretValue", "sns:Publish",
        "logs:CreateLogStream", "logs:PutLogEvents"}
    assert f"log-group:/aws/lambda/{function}:*" in json.dumps(fn)
    # The deploy policy cannot make or widen a role, and passes only the function's role to Lambda.
    admin = json.loads((INFRA / "iam" / "deploy-admin-policy.json").read_text())
    deny = next(st for st in admin["Statement"] if st["Effect"] == "Deny")
    assert {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"} <= set(deny["Action"])
    passes = [st for st in admin["Statement"] if "iam:PassRole" in json.dumps(st.get("Action"))]
    assert len(passes) == 1 and passes[0]["Resource"].endswith(":role/llm-gateway-fn")
    assert not [st for st in admin["Statement"] if st["Effect"] == "Allow" and "iam:" in json.dumps(st["Action"])
                and st is not passes[0]]


def test_clean_up_commands_cover_what_the_deploy_creates():
    """README, Clean up: every resource the template keeps or the steps create by hand has a delete command."""
    readme = (INFRA.parent / "README.md").read_text()
    block = readme.split("### Clean up", 1)[1].split("```", 2)[1]
    template = (INFRA / "template.yaml").read_text()
    assert "sam delete --stack-name llm-gateway" in block
    # Tables with DeletionPolicy: Retain outlive `sam delete`; each one is deleted by name.
    retained = re.findall(r"DeletionPolicy: Retain\n.*?TableName: (\S+)", template, flags=re.S)
    assert sorted(retained) == ["llm-gateway-keys", "llm-gateway-texts", "llm-gateway-usage"]
    for table in retained:
        assert f"delete-table --region $REGION --table-name {table}" in block
    # Everything else in the template goes with the stack: nothing else is retained.
    assert template.count("DeletionPolicy: Retain") == len(retained)
    assert "LogGroupName: /aws/lambda/llm-gateway" in template   # in the stack, so `sam delete` removes it
    # Made by hand in the deploy steps: the role with its inline policy, and the secret.
    for made, removed in (("aws iam create-role --role-name llm-gateway-fn", "aws iam delete-role --role-name llm-gateway-fn"),
                          ("aws iam put-role-policy --role-name llm-gateway-fn", "aws iam delete-role-policy --role-name llm-gateway-fn"),
                          ("create-secret --region $REGION --name llm-gateway/upstream-key",
                           "delete-secret --region $REGION --secret-id llm-gateway/upstream-key")):
        assert made in readme and removed in block, made
    # A secret deleted with a recovery window keeps its name taken, so the next deploy's create-secret fails.
    assert "--force-delete-without-recovery" in block
