"""Remove what ext-sdk-lambda's deploy created for this PREFIX; the lake bucket stays (scripts/lake.py down owns it).

  uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py          list what it would delete
  uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py --yes    delete it

Removes, by name: the function URL and the function `<PREFIX>-agent-sdk`, its log group, the parameter
`<PREFIX>-agent-sdk-token`, the role `<PREFIX>-agent-sdk-role` (its inline policy first) and the objects under
`code/agent-sdk/` of `<PREFIX>-lake-<account>`. Each delete is written to the ledger. Every step is tried, also
after one fails; a failure is printed and the exit code is 1. Something already gone is reported `absent`.
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.append(str(HERE))

import lake  # noqa: E402
from settings import load_settings  # noqa: E402
from tags import record  # noqa: E402

REGION = "ca-central-1"
CODE_PREFIX = "code/agent-sdk/"

# Error codes that mean "already gone": such a step is reported as absent, not as a failure.
GONE = {"ResourceNotFoundException", "ResourceNotFound", "NotFoundException", "NotFound", "404", "NoSuchEntity",
        "NoSuchBucket", "EntityNotFoundException", "QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue"}


class Steps:
    """Runs every clean-up step, also after one fails, and keeps what happened to each (README, Cost and clean-up).

    removed: deleted now (with dry_run: would be deleted). absent: was already gone. failed: the call was refused or
    broke; the text says why. A clean-up with anything in `failed` must exit non-zero, so nobody reads it as done.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.removed: list[str] = []
        self.absent: list[str] = []
        self.failed: list[str] = []

    def _note(self, what: str, err: Exception) -> None:
        code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if code in GONE:
            self.absent.append(what)
        else:
            self.failed.append(f"{what}: {code or type(err).__name__}: {err}")

    def do(self, what: str, fn=None, **kw) -> bool:
        """One delete call. True when it went through (or would, with dry_run)."""
        try:
            if fn and not self.dry_run:
                fn(**kw)
        except (ClientError, BotoCoreError, TimeoutError) as err:
            self._note(what, err)
            return False
        self.removed.append(what)
        return True

    @contextmanager
    def part(self, what: str):
        """A group of look-ups and deletes for one service: an error inside ends this part only."""
        try:
            yield
        except (ClientError, BotoCoreError, TimeoutError) as err:
            self._note(what, err)

    def report(self, log=print) -> int:
        """Print one line per step; return the exit code (1 when any step failed)."""
        verb = "would remove" if self.dry_run else "removed"
        for kind, items in ((verb, self.removed), ("absent", self.absent), ("FAILED", self.failed)):
            for item in items:
                log(f"{kind:<8} {item}")
        return 1 if self.failed else 0


def clean(s, yes: bool, sess=None) -> Steps:
    sess = sess or boto3.session.Session(region_name=REGION)
    st = Steps(dry_run=not yes)
    lam, logs, ssm, iam, s3 = (sess.client(n) for n in ("lambda", "logs", "ssm", "iam", "s3"))

    def done(type_, name, ok):
        if ok and yes:
            record(type_, name, "deleted", script="ext-sdk-lambda/clean_up.py --yes")

    name = s.function_name
    done("lambda-function-url", name, st.do(f"function URL of {name}", lam.delete_function_url_config,
                                            FunctionName=name))
    done("lambda-function", name, st.do(f"Lambda function {name}", lam.delete_function, FunctionName=name))
    done("log-group", s.log_group, st.do(f"log group {s.log_group}", logs.delete_log_group,
                                         logGroupName=s.log_group))
    with st.part(f"parameter {s.token_param}"):
        # DeleteParameter answers a missing parameter with ParameterNotFound, a code GONE does not list: look first
        found = ssm.describe_parameters(ParameterFilters=[{"Key": "Name", "Values": [s.token_param]}])["Parameters"]
        if not found:
            st.absent.append(f"parameter {s.token_param}")
        else:
            done("ssm-parameter", s.token_param, st.do(f"parameter {s.token_param}", ssm.delete_parameter,
                                                       Name=s.token_param))
    with st.part(f"IAM role {s.role_name}"):
        for policy in iam.list_role_policies(RoleName=s.role_name)["PolicyNames"]:
            st.do(f"inline policy {policy} of {s.role_name}", iam.delete_role_policy, RoleName=s.role_name,
                  PolicyName=policy)
        done("iam-role", s.role_name, st.do(f"IAM role {s.role_name}", iam.delete_role, RoleName=s.role_name))
    with st.part(f"objects under {CODE_PREFIX}"):
        bucket = lake.bucket_name(sess)
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=CODE_PREFIX)
                for o in page.get("Contents", [])]
        for key in keys:
            st.do(f"s3://{bucket}/{key}", s3.delete_object, Bucket=bucket, Key=key)
        if keys:
            done("s3-prefix", f"{bucket}/{CODE_PREFIX}", True)
    return st


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, only list")
    a = p.parse_args(argv)
    os.environ.setdefault("AWS_REGION", REGION)
    st = clean(load_settings(), a.yes)
    code = st.report()
    if not a.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())
