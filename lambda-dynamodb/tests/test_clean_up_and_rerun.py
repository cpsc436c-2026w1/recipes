"""The clean-up when AWS refuses a delete, and main/run.sh over an API that an earlier KEEP=1 run left.

The scripts run for real (no DRY) with stand-ins on the PATH for `aws`, `curl`, `python3` and `sleep`: `aws` writes
each call to a log and answers what the test tells it to. Nothing reaches AWS and nothing waits.
"""

import os
import subprocess

import pytest

from conftest import RECIPE
from harness import FAKE_AWS

@pytest.fixture
def fake(tmp_path):
    """fake(script, **env) -> (CompletedProcess, the aws calls made)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "aws.log"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(FAKE_AWS)
    for tool in ("curl", "python3", "sleep"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)

    def _run(script, bare=False, **extra):
        log.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and k not in ("KEEP", "DRY", "ROLE_ARN")}
        env |= {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path), "FAKE_AWS_LOG": str(log),
                "PREFIX": "recipe"} | extra
        args = ["--yes"] if script == "clean_up.sh" and not bare else []     # a clean-up deletes only with --yes
        p = subprocess.run(["bash", str(RECIPE / script), *args], env=env, capture_output=True, text=True, timeout=60)
        return p, log.read_text().splitlines()

    return _run


DELETES = ["lambda delete-function-url-config", "lambda delete-function ", "logs delete-log-group",
           "iam delete-role-policy", "iam delete-role "]


def test_a_refused_delete_is_reported_and_the_other_deletes_still_run(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_REFUSE="lambda delete-function --function-name")
    assert p.returncode == 1
    assert "FAILED: the function was not deleted" in p.stderr and "NOT complete" in p.stderr
    assert "(no function: already gone)" not in p.stdout     # a refusal is not "already gone"
    for delete in DELETES:
        assert any(delete in c for c in calls), delete
    assert any("list-functions" in c for c in calls)         # the checks at the end still ran


def test_what_is_already_gone_is_not_a_failure(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_REFUSE="delete-", FAKE_AWS_CODE="ResourceNotFoundException")
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("already gone") == 5 and "FAILED" not in p.stderr
    p, calls = fake("clean_up.sh", FAKE_AWS_REFUSE="iam delete-", FAKE_AWS_CODE="NoSuchEntity")
    assert p.returncode == 0 and p.stdout.count("already gone") == 2


def test_the_clean_up_checks_that_the_log_group_is_gone(fake):
    p, calls = fake("clean_up.sh")
    assert p.returncode == 0, p.stderr
    assert any("logs delete-log-group --log-group-name /aws/lambda/recipe-api" in c for c in calls)
    assert "logs describe-log-groups --log-group-name-prefix /aws/lambda/recipe-api" in calls[-3]
    assert "logGroups[?logGroupName=='/aws/lambda/recipe-api']" in calls[-3]       # exact: a teammate's group stays out
    assert "iam list-roles" in calls[-2] and "Roles[?RoleName=='recipe-api-role']" in calls[-2]
    assert "lambda list-functions" in calls[-1] and "Functions[?FunctionName=='recipe-api']" in calls[-1]
    assert sum(line.startswith("(no ") and line.endswith(" left)") for line in p.stdout.splitlines()) == 3


def test_something_still_there_after_the_clean_up_is_a_failure(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_LEFT_IN="describe-log-groups", FAKE_AWS_LEFT="/aws/lambda/recipe-api")
    assert p.returncode == 1
    assert "FAILED: still there: log group /aws/lambda/recipe-api" in p.stderr


def test_with_your_own_role_the_role_is_not_checked(fake):
    p, calls = fake("clean_up.sh", ROLE_ARN="arn:aws:iam::111122223333:role/mine")
    assert p.returncode == 0, p.stderr
    assert not any("iam list-roles" in c or "iam delete-role" in c for c in calls)


def test_a_run_whose_clean_up_is_refused_ends_with_an_error(fake):
    p, calls = fake("main/run.sh", FAKE_AWS_REFUSE="iam delete-role --role-name")
    assert p.returncode == 1 and "FAILED: the role was not deleted" in p.stderr


def test_a_first_run_creates_everything_once_and_removes_it(fake):
    p, calls = fake("main/run.sh")
    assert p.returncode == 0, p.stdout + p.stderr
    for create in ("iam create-role", "iam put-role-policy", "lambda create-function ", "create-function-url-config"):
        assert sum(create in c for c in calls) == 1, create
    assert sum("add-permission" in c for c in calls) == 2
    made = next(i for i, c in enumerate(calls) if "create-function-url-config" in c)
    for delete in DELETES:
        assert any(delete in c for c in calls[made:]), delete


def test_a_second_run_after_keep_uses_what_is_there(fake):
    p, calls = fake("main/run.sh", KEEP="1", FAKE_AWS_EXISTS="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.count("already there from an earlier run: kept") == 5    # log group, role, URL, two permissions
    assert not any("lambda create-function " in c for c in calls)
    assert any("lambda update-function-code" in c for c in calls)            # the current handler is deployed
    assert any("lambda wait function-updated-v2" in c for c in calls)
    assert not any("delete-" in c for c in calls)                            # KEEP=1


def test_a_create_that_is_refused_for_another_reason_stops_the_run(fake):
    p, calls = fake("main/run.sh", FAKE_AWS_REFUSE="iam create-role")
    assert p.returncode != 0 and "AccessDeniedException" in p.stderr
    assert not any("lambda create-function " in c for c in calls)


def test_a_create_refused_while_an_update_is_in_progress_is_tried_again(fake):
    p, calls = fake("main/run.sh", FAKE_AWS_BUSY="2")
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.count("in progress: trying again") == 2
    assert sum("add-permission" in c for c in calls) == 4                 # two refused, then the two permissions


def test_a_create_that_stays_in_progress_stops_the_run(fake):
    p, calls = fake("main/run.sh", FAKE_AWS_BUSY="99")
    assert p.returncode != 0 and "An update is in progress" in p.stderr
    assert sum("add-permission" in c for c in calls) == 6                 # CREATE_TRIES, then it gives up
    assert "already there" not in p.stdout


def test_a_conflict_that_does_not_say_already_exists_stops_the_run(fake):
    p, calls = fake("main/run.sh", FAKE_AWS_REFUSE="create-function-url-config", FAKE_AWS_CODE="ResourceConflictException",
                    FAKE_AWS_MESSAGE="something else is wrong")
    assert p.returncode != 0 and "something else is wrong" in p.stderr
    assert "already there" not in p.stdout
    assert not any("add-permission" in c for c in calls)


def test_a_bare_clean_up_deletes_nothing_and_says_so(fake):
    """The clean-up contract: without --yes the script lists the commands and calls aws for none of them."""
    p, calls = fake("clean_up.sh", bare=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not [c for c in calls if " delete-" in f" {c}"], calls
    assert p.stdout.rstrip().endswith("Nothing deleted. Run again with --yes to delete these.")


def test_clean_up_with_yes_deletes(fake):
    p, calls = fake("clean_up.sh")
    assert [c for c in calls if " delete-" in f" {c}"], calls
    assert "Nothing deleted. Run again with --yes to delete these." not in p.stdout
