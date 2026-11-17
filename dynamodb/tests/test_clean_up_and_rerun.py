"""clean_up.sh when AWS refuses a delete, and the scripts over tables that exist or are still being deleted.

moto does not refuse a call and has no DELETING state, so these tests put a stand-in `aws` on the PATH: it writes
each call to a log and answers what the test tells it to. Nothing reaches AWS.
"""

import os
import subprocess

import pytest

from conftest import RECIPE, _base_env
from harness import FAKE_AWS, put_on_path

@pytest.fixture
def fake(tmp_path):
    """fake(script, **env) -> (CompletedProcess, the aws calls made). Runs `bash <script>` for real (no DRY)."""
    bin_dir, log = put_on_path(tmp_path, FAKE_AWS), tmp_path / "aws.log"

    def _run(script, **extra):
        log.write_text("")
        env = _base_env(tmp_path) | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(log)} | extra
        p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=60)
        return p, log.read_text().splitlines()

    return _run


def test_a_refused_delete_is_reported_and_the_other_steps_still_run(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_TABLE_STATUS="ACTIVE", FAKE_AWS_REFUSE="delete-table --table-name recipe-trips",
                    BUCKET="my-bucket")
    assert p.returncode == 1
    assert "FAILED: table recipe-trips was not deleted" in p.stderr and "NOT complete" in p.stderr
    assert "not found" not in p.stdout                       # a refusal is not "not found"
    for later in ("delete-table --table-name recipe-rider-trips", "delete-table --table-name recipe-drivers",
                  "glue delete-table --database-name recipe_lake", "glue delete-database --name recipe_lake",
                  "s3 rm --recursive --quiet s3://my-bucket/recipe/exports/",
                  "s3 rm --recursive --quiet s3://my-bucket/recipe/athena-results/", "dynamodb list-tables"):
        assert any(later in c for c in calls), later


def test_a_refused_s3_delete_is_reported_and_the_athena_results_are_still_tried(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_REFUSE="recipe/exports/", FAKE_AWS_CODE="AccessDenied", BUCKET="my-bucket")
    assert p.returncode == 1 and "FAILED: the export in s3://my-bucket/recipe/exports/" in p.stderr
    assert any("recipe/athena-results/" in c for c in calls)


def test_what_is_already_gone_is_not_a_failure(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_REFUSE="delete-", FAKE_AWS_CODE="ResourceNotFoundException")
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("not found: already gone") == 5    # three tables, the Athena table, its database
    assert "FAILED" not in p.stderr


def test_each_deleted_table_is_waited_out(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_TABLE_STATUS="ACTIVE")
    assert p.returncode == 0, p.stderr
    for t in ("recipe-trips", "recipe-rider-trips", "recipe-drivers"):
        deleted = next(i for i, c in enumerate(calls) if f"delete-table --table-name {t} " in c)
        assert f"dynamodb wait table-not-exists --table-name {t}" in calls[deleted + 1]


def test_a_table_that_is_already_being_deleted_is_not_deleted_again(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_TABLE_STATUS="DELETING")
    assert p.returncode == 0, p.stderr
    assert not any("dynamodb delete-table" in c for c in calls)
    assert sum("wait table-not-exists" in c for c in calls) == 3
    assert p.stdout.count("already being deleted") == 3


def test_without_a_bucket_the_clean_up_says_what_stays_in_s3(fake):
    p, calls = fake("clean_up.sh")
    assert p.returncode == 0 and "BUCKET is not set" in p.stdout
    assert not any(c.startswith("s3 ") for c in calls)
    assert any("glue delete-database --name recipe_lake" in c for c in calls)   # the database needs no bucket


def test_a_second_run_keeps_the_table_that_is_there(fake):
    p, calls = fake("main/run.sh", KEEP="1", FAKE_AWS_TABLE_STATUS="ACTIVE")
    assert p.returncode == 0, p.stdout + p.stderr
    assert not any("create-table" in c for c in calls)
    assert "already there: kept as it is" in p.stdout
    assert sum("batch-write-item" in c for c in calls) == 2  # the scenario itself still ran


def test_a_run_right_after_a_clean_up_waits_for_the_deleting_table_then_creates_it(fake):
    p, calls = fake("main/run.sh", KEEP="1", FAKE_AWS_TABLE_STATUS="DELETING")
    assert p.returncode == 0, p.stdout + p.stderr
    waited = next(i for i, c in enumerate(calls) if "wait table-not-exists --table-name recipe-trips" in c)
    created = next(i for i, c in enumerate(calls) if "create-table --table-name recipe-trips" in c)
    assert waited < created


def test_a_first_run_creates_the_table(fake):
    p, calls = fake("main/run.sh", KEEP="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert sum("create-table --table-name recipe-trips" in c for c in calls) == 1


def test_a_delete_that_fails_without_a_message_is_a_failure(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_SILENT_FAIL="1", BUCKET="my-bucket")
    assert p.returncode == 1
    assert "FAILED: the export in s3://my-bucket/recipe/exports/ was not deleted" in p.stderr
    assert "already gone" not in p.stdout.split("== 2.")[1]


def test_the_final_checks_name_the_exact_tables_and_database(fake):
    p, calls = fake("clean_up.sh")
    assert p.returncode == 0, p.stderr
    tables = next(c for c in calls if "dynamodb list-tables" in c)
    assert "@=='recipe-trips' || @=='recipe-rider-trips' || @=='recipe-drivers'" in tables
    assert "DatabaseList[?Name=='recipe_lake']" in next(c for c in calls if "glue get-databases" in c)
    assert p.stdout.count("(no table left)") == 1 and p.stdout.count("(no database left)") == 1


def test_a_table_still_there_after_the_clean_up_is_a_failure(fake):
    p, calls = fake("clean_up.sh", FAKE_AWS_LEFT_IN="list-tables", FAKE_AWS_LEFT="recipe-drivers")
    assert p.returncode == 1
    assert "FAILED: still there: table recipe-drivers" in p.stderr and "NOT complete" in p.stderr
