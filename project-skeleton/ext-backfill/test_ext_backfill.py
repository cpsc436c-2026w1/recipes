"""Tests of two ext-backfill guards, offline (DuckDB, moto); run them from project-skeleton with
`uv run pytest -q ext-backfill/test_ext_backfill.py` (the folder's own `uv run pytest -q` collects tests/ only).

1. A trips_history copied from one source is not rewritten from another without --replace-source: the copy deletes a
   day whose count differs and copies it again, so a second source would silently replace the first one's days.
2. clean_up.py keeps a <PREFIX>-trips-by-id that existed before the load (recorded borrowed): batch writes added
   items to it, and deleting it would delete the live items too.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent
ROOT = EXT.parent
for folder in (EXT, ROOT / "scripts", ROOT):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(f"ext_backfill_{name}", EXT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PREFIX", "recipe")
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    return tmp_path


def test_a_second_source_does_not_replace_the_first_ones_days(here, capsys):
    backfill = load_module("backfill")
    days = ["--from", "2024-03-14", "--to", "2024-03-15"]
    assert backfill.main(["--source", "course", *days]) == 0
    first = capsys.readouterr().out
    assert "counts   2 days, 0 differ; 67 rows in trips_history" in first
    files = ["--source", "files", "--files", str(EXT / "sim" / "SIM04"), *days]
    with pytest.raises(SystemExit, match="trips_history was copied from course, not files:"):
        backfill.main(files)
    assert backfill.main(["--check-only", *days]) == 0                    # the course's 67 rows are still there
    assert "67 rows in trips_history" in capsys.readouterr().out
    assert backfill.main([*files, "--replace-source"]) == 0               # asked for: the days are replaced
    assert "6 rows in trips_history" in capsys.readouterr().out
    assert backfill.main(files) == 0                                       # the files are now the recorded source


def test_clean_up_keeps_a_trips_by_id_table_that_was_there_before(here):
    import boto3
    import tags
    from skeleton.offline import fake_aws

    clean = load_module("clean_up")
    with fake_aws():
        sess = boto3.Session(region_name="ca-central-1")
        account = sess.client("sts").get_caller_identity()["Account"]
        sess.client("s3").create_bucket(Bucket=f"recipe-lake-{account}",
                                        CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
        ddb = sess.client("dynamodb")
        ddb.create_table(TableName="recipe-trips-by-id", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "trip_id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "trip_id", "KeyType": "HASH"}])
        tags.record("dynamodb-table", "recipe-trips-by-id", "borrowed", script="ext-backfill/backfill.py --to-dynamodb")
        lines = []
        assert clean.clean_aws(sess, yes=True, log=lines.append) == 0
        assert "recipe-trips-by-id" in ddb.list_tables()["TableNames"]
        assert any(line.startswith("kept     DynamoDB table recipe-trips-by-id") for line in lines)


def test_to_dynamodb_before_any_copy_stops_with_what_to_run_first(here):
    backfill = load_module("backfill")
    with pytest.raises(SystemExit, match="trips_history has no rows: copy the history first"):
        backfill.main(["--to-dynamodb"])
