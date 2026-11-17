"""Limits that live outside the app's code: an AWS Budgets budget, and the Lambda function's concurrency and timeout.

A bug in the app cannot raise these limits; only someone who changes the account's settings can
(README, "Decisions": limits outside the code path; Agentic AI Lens AGENTCOST07-BP01).
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from config import (BUDGET_ALERTS, LAMBDA_MEMORY_MB, LAMBDA_TIMEOUT_S, MONTHLY_BUDGET_USD, RESERVED_CONCURRENCY,
                    Settings)

MAIN = Path(__file__).resolve().parent
UNRESERVED_MINIMUM = 100  # Lambda keeps 100 units of concurrency unreserved in every account and Region
LOGS_POLICY_NAME = "write-own-logs"
RECIPE_TAG = {"Key": "recipe", "Value": "limits-and-alarms"}


def not_ours(kind: str, name: str) -> str:
    """Why a resource with the recipe's name is not touched: it does not carry the recipe's tag."""
    return (f"{kind} {name} exists but does not carry the tag {RECIPE_TAG['Key']}={RECIPE_TAG['Value']}, so this "
            f"recipe did not create it and does not change or delete it. Use another PREFIX, or delete or tag it "
            f"yourself if it is yours")


def function_is_ours(lam, name: str) -> bool:
    """The function exists (else ResourceNotFoundException) and carries the recipe's tag.

    Tags are read with ListTags, not from GetFunction: GetFunction returns tags only to a caller allowed
    lambda:ListTags and otherwise answers without them, which would read as "not ours"
    (https://docs.aws.amazon.com/lambda/latest/api/API_GetFunction.html). ListTags without that permission is an
    AccessDenied, which the caller reports as a failed step."""
    arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
    tags = lam.list_tags(Resource=arn).get("Tags") or {}
    return tags.get(RECIPE_TAG["Key"]) == RECIPE_TAG["Value"]


def budget_arn(account_id: str, name: str) -> str:
    return f"arn:aws:budgets::{account_id}:budget/{name}"


def budget_is_ours(budgets, account_id: str, name: str) -> bool:
    """AWS Budgets keeps resource tags on a budget (CreateBudget ResourceTags, ListTagsForResource on its ARN)."""
    tags = budgets.list_tags_for_resource(ResourceARN=budget_arn(account_id, name)).get("ResourceTags", [])
    return RECIPE_TAG in tags


# --- IAM: the function's role ------------------------------------------------------------------------------------

def logs_policy(s: Settings, account_id: str) -> dict:
    """Write access to the function's own log group, nothing else (the EMF lines are log lines)."""
    group = f"arn:aws:logs:{s.region}:{account_id}:log-group:/aws/lambda/{s.function_name}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "logs:CreateLogGroup", "Resource": group},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"{group}:*"}]}


def ensure_role(s: Settings, iam, account_id: str) -> tuple[str, str]:
    """Create {PREFIX}-app-role for the function, or reuse it. Returns (role ARN, line to print).

    README, "Decisions" (IAM): the role may only write the function's logs. Set LAMBDA_ROLE_ARN to use a role you
    already have instead; then this function is not called and clean_up.py leaves that role alone."""
    policy = json.dumps(logs_policy(s, account_id))
    try:
        role = iam.get_role(RoleName=s.role_name)["Role"]
        if RECIPE_TAG not in iam.list_role_tags(RoleName=s.role_name)["Tags"]:
            # Someone else's role with this name: its permissions are unknown, so the function is not given it.
            raise SystemExit(not_ours("iam role", s.role_name))
        # A run that stopped between CreateRole and PutRolePolicy left the role without its policy, and the
        # function then writes no log line and no metric. PutRolePolicy replaces: safe to repeat.
        iam.put_role_policy(RoleName=s.role_name, PolicyName=LOGS_POLICY_NAME, PolicyDocument=policy)
        return role["Arn"], f"role {s.role_name} already there"
    except iam.exceptions.NoSuchEntityException:
        pass
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "lambda.amazonaws.com"}}]}
    arn = iam.create_role(RoleName=s.role_name, AssumeRolePolicyDocument=json.dumps(trust),
                          Tags=[RECIPE_TAG])["Role"]["Arn"]
    iam.put_role_policy(RoleName=s.role_name, PolicyName=LOGS_POLICY_NAME, PolicyDocument=policy)
    return arn, f"role {s.role_name} created: writes its function's logs only"


# --- AWS Budgets --------------------------------------------------------------------------------------------------

def budget_spec(s: Settings, account_id: str, topic_arn: str) -> dict:
    """A monthly cost budget for the whole account, with alerts to the alarm topic."""
    return {
        "AccountId": account_id,
        "Budget": {
            "BudgetName": s.budget_name,
            "BudgetLimit": {"Amount": f"{MONTHLY_BUDGET_USD:.2f}", "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        },
        "NotificationsWithSubscribers": [
            {"Notification": {"NotificationType": kind, "ComparisonOperator": "GREATER_THAN",
                              "Threshold": pct, "ThresholdType": "PERCENTAGE"},
             "Subscribers": [{"SubscriptionType": "SNS", "Address": topic_arn}]}
            for kind, pct in BUDGET_ALERTS],
        # The recipe's tag: a second run and clean_up.py touch only a budget that carries it.
        "ResourceTags": [RECIPE_TAG],
    }


def ensure_budget(s: Settings, budgets, account_id: str, topic_arn: str) -> str:
    """AWS Budgets reads billing data that is updated at least once a day, so its alerts can come a day late:
    it catches spending that goes on for days, not a loop that runs for a minute (README, "Decisions")."""
    try:
        budgets.describe_budget(AccountId=account_id, BudgetName=s.budget_name)
        if not budget_is_ours(budgets, account_id, s.budget_name):
            raise SystemExit(not_ours("budget", s.budget_name))
        return f"budget {s.budget_name} already there"
    except budgets.exceptions.NotFoundException:
        budgets.create_budget(**budget_spec(s, account_id, topic_arn))
        return f"budget {s.budget_name} created: {MONTHLY_BUDGET_USD:.2f} USD a month"


# --- Lambda: deploy the app, cap its concurrency and its run time ---------------------------------------------

def app_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("app.py", "emf.py", "config.py"):
            z.write(MAIN / name, name)
    return buf.getvalue()


def ensure_function(s: Settings, lam, role_arn: str, wait_s: float = 3.0) -> str:
    env = {"Variables": {"METRIC_NAMESPACE": s.namespace, "SERVICE_NAME": s.service}}
    try:
        if not function_is_ours(lam, s.function_name):
            # Someone else's function with this name: its code is not replaced.
            raise SystemExit(not_ours("lambda function", s.function_name))
        lam.update_function_code(FunctionName=s.function_name, ZipFile=app_zip())
        lam.get_waiter("function_updated_v2").wait(FunctionName=s.function_name)
        lam.update_function_configuration(FunctionName=s.function_name, Timeout=LAMBDA_TIMEOUT_S,
                                          MemorySize=LAMBDA_MEMORY_MB, Environment=env)
        return f"function {s.function_name} updated"
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):
            try:
                lam.create_function(
                    FunctionName=s.function_name, Runtime="python3.13", Architectures=["arm64"], Role=role_arn,
                    Handler="app.handler", Code={"ZipFile": app_zip()},
                    # README, "Decisions": the timeout is a deadline the code cannot skip.
                    Timeout=LAMBDA_TIMEOUT_S, MemorySize=LAMBDA_MEMORY_MB, Environment=env,
                    Tags={RECIPE_TAG["Key"]: RECIPE_TAG["Value"]})
                break
            except lam.exceptions.InvalidParameterValueException as e:
                # A new role takes a few seconds to reach Lambda ("cannot be assumed"); wait and try again.
                if "role" not in str(e).lower() or attempt == 9:
                    raise
                time.sleep(wait_s)
        lam.get_waiter("function_active_v2").wait(FunctionName=s.function_name)
        return f"function {s.function_name} created: timeout {LAMBDA_TIMEOUT_S} s"


def concurrency_fits(account_settings: dict, wanted: int, current: int = 0) -> bool:
    """Reserving `wanted` must leave at least 100 units unreserved (Lambda API Reference, PutFunctionConcurrency).
    `UnreservedConcurrentExecutions` is the total minus what other functions reserved. A course sandbox reported 200
    unreserved on 2026-11-12, so 5 fits there. An account whose total is below 105 cannot reserve 5; the function then
    runs with the account's limit only."""
    unreserved = account_settings["AccountLimit"]["UnreservedConcurrentExecutions"] + current
    return unreserved - wanted >= UNRESERVED_MINIMUM


def ensure_concurrency(s: Settings, lam, account_settings: dict | None = None) -> str:
    """`account_settings` is the GetAccountSettings reply; the offline run passes one, since moto lacks the call."""
    settings = account_settings or lam.get_account_settings()
    current = lam.get_function_concurrency(FunctionName=s.function_name).get("ReservedConcurrentExecutions", 0)
    if not concurrency_fits(settings, RESERVED_CONCURRENCY, current):
        total = settings["AccountLimit"]["ConcurrentExecutions"]
        return (f"reserved concurrency NOT set: the account's total is {total}, and Lambda keeps 100 unreserved. "
                f"Ask for a quota increase, or rely on the alarms and the gateway budget")
    # README, "Decisions": reserved concurrency is a cap the app cannot exceed, set outside the code.
    lam.put_function_concurrency(FunctionName=s.function_name, ReservedConcurrentExecutions=RESERVED_CONCURRENCY)
    return f"reserved concurrency {RESERVED_CONCURRENCY} on {s.function_name}"
