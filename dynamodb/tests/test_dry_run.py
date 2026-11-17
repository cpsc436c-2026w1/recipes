"""Every scenario under DRY=1: the commands it would run, checked against the decisions in README.md."""

import json
import re

from conftest import RECIPE, commands


def test_main_prints_every_step_and_cleans_up(dry):
    out = dry("main/run.sh").stdout
    cmds = commands(out)
    # Decision (keys): one table, partition key trip_id, billed per request.
    create = next(c for c in cmds if c.startswith("aws dynamodb create-table"))
    assert "--table-name recipe-trips" in create and "PAY_PER_REQUEST" in create
    assert "AttributeName=trip_id,KeyType=HASH" in create
    # Decision (conditional write): both drivers send the same condition; only one can pass it.
    accepts = [c for c in cmds if "update-item" in c and "attribute_not_exists(driver_id)" in c]
    assert len(accepts) == 2 and '"d-7"' in accepts[0] and '"d-9"' in accepts[1]
    # Decision (consistency): the read after the accept is strong.
    assert "--consistent-read" in [c for c in cmds if c.startswith("aws dynamodb get-item")][-1]
    # Without KEEP=1 the scenario ends with the clean-up: the three tables, then the check that none is left.
    assert [c for c in cmds if "delete-table --table-name" in c] == [
        f"aws dynamodb delete-table --table-name {t} --query TableDescription.TableStatus"
        for t in ("recipe-trips", "recipe-rider-trips", "recipe-drivers")]
    assert cmds[-2].startswith("aws dynamodb list-tables") and cmds[-1].startswith("aws glue get-databases")


def test_keep_skips_the_clean_up(dry):
    out = dry("main/run.sh", KEEP="1").stdout
    assert "delete-table" not in out and "bash clean_up.sh" in out


def test_prefix_names_every_table(dry):
    for script in ("main/run.sh", "ext-riders-and-drivers/run.sh", "clean_up.sh"):
        out = dry(script, PREFIX="recipe-ana").stdout
        names = set(re.findall(r"--table-name (\S+)", out))
        assert names and all(n.startswith("recipe-ana-") for n in names), (script, names)


def test_region_defaults_to_ca_central_1(dry, tmp_path):
    import subprocess

    p = subprocess.run(["bash", "-c", f"source {RECIPE / 'lib.sh'}; echo $AWS_REGION"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert p.stdout.strip() == "ca-central-1"


def test_riders_and_drivers_keys_and_transactions(dry, tmp_path):
    cmds = commands(dry("ext-riders-and-drivers/run.sh", KEEP="1").stdout)
    rider = next(c for c in cmds if "create-table --table-name recipe-rider-trips" in c)
    assert '"AttributeName":"rider_id","KeyType":"HASH"' in rider
    assert '"AttributeName":"requested_at","KeyType":"RANGE"' in rider
    # Decision (transaction): four transactions (two requests, two accepts), each from a file.
    assert sum(c.startswith("aws dynamodb transact-write-items") for c in cmds) == 4
    # The last file written is d-3 accepting t-0101: both items carry their own condition.
    tx = json.loads((tmp_path / "recipe-tx.json").read_text())
    conds = [item["Update"]["ConditionExpression"] for item in tx]
    assert conds == ["attribute_not_exists(driver_id)", "#s = :available"]
    # Decision (optimistic locking): both fare writes are conditional on the version the screen read.
    fares = [c for c in cmds if "fare_quote = :f" in c]
    assert len(fares) == 2 and all("version = :seen" in c for c in fares)


def test_nightly_report_turns_on_pitr_before_the_export(dry):
    cmds = commands(dry("ext-nightly-report/run.sh", BUCKET="my-bucket", KEEP="1").stdout)
    pitr = next(i for i, c in enumerate(cmds) if "update-continuous-backups" in c)
    export = next(i for i, c in enumerate(cmds) if "export-table-to-point-in-time" in c)
    assert pitr < export and "--s3-bucket my-bucket --s3-prefix recipe/exports" in cmds[export]


def test_clean_up_touches_only_prefixed_names(dry):
    plain_out = dry("clean_up.sh").stdout
    plain = commands(plain_out)
    assert not any(c.startswith("aws s3") for c in plain)  # S3 only when BUCKET is set
    assert "BUCKET is not set" in plain_out                # and the script says what then stays in S3
    assert "aws glue delete-database --name recipe_lake" in plain      # the Athena database needs no bucket
    cmds = commands(dry("clean_up.sh", BUCKET="my-bucket").stdout)
    s3 = [c for c in cmds if c.startswith("aws s3 rm")]
    assert s3 == ["aws s3 rm --recursive --quiet s3://my-bucket/recipe/exports/",
                  "aws s3 rm --recursive --quiet s3://my-bucket/recipe/athena-results/"]
