"""Delete the demonstration system that scripts/demo_up.py made, the hourly stream first.

    uv run python clean_up.py            # what it would delete; nothing is deleted (PREFIX exported first)
    uv run python clean_up.py --yes      # delete it

Only what the ledger records `created` is deleted; a `borrowed` row is kept. The bucket and the workgroup
<PREFIX>-wg are left to `scripts/lake.py down`, which the teardown runs last. Every step is attempted; a refused one
is printed FAILED and the exit code is 1. Running it twice is safe.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from demo_up import names  # noqa: E402
from kinds import KINDS, label  # noqa: E402
from tags import ledger_rows, record, recorded  # noqa: E402

ORDER = ("kinesis-stream", "lambda-esm", "lambda-function", "log-group", "dynamodb-table", "iam-role", "glue-table",
         "glue-database")          # hourly first; the mapping before its function; tables before their database


def owned() -> list[tuple[str, str, str]]:
    """(kind, name, id) of each demonstration resource the ledger records `created`, in delete order."""
    n = names()
    mine = {("kinesis-stream", n["stream"]), ("lambda-function", n["function"]), ("log-group", n["log_group"]),
            ("dynamodb-table", n["table"]), ("iam-role", n["role"]), ("glue-database", n["database"]),
            ("glue-table", f"{n['database']}.{n['glue_table']}"), ("lambda-esm", f"{n['stream']} to {n['function']}")}
    last = {}
    for r in ledger_rows():
        last[(r["type"], r["name"])] = r
    out = []
    for kind in ORDER:
        for (k, name), r in last.items():
            if k == kind and (k, name) in mine and r["state"] == "created":
                ident = r["arn"].rsplit(":", 1)[-1] if k == "lambda-esm" and r.get("arn") else name
                out.append((k, name, ident))
    return out


def clean(sess=None, yes: bool = False, log=print) -> int:
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    failed = 0
    for kind, name, ident in owned():
        what = f"{label(kind)} {name}"
        try:
            if not KINDS[kind].exists(sess, ident):
                log(f"absent   {what}")
                if yes:
                    record(kind, name, "deleted", script="clean_up.py")
                continue
            if yes:
                KINDS[kind].delete(sess, ident)
                record(kind, name, "deleted", script="clean_up.py")
            log(f"{'removed' if yes else 'would remove':<8} {what}")
        except (ClientError, BotoCoreError) as err:
            failed += 1
            log(f"FAILED   {what}: {getattr(err, 'response', {}).get('Error', {}).get('Code', type(err).__name__)}")
    kept = [(r["type"], r["name"]) for r in ledger_rows() if r["state"] == "borrowed"]
    for kind, name in dict.fromkeys(kept):
        if recorded(kind, name) == "borrowed":
            log(f"kept     {label(kind)} {name} (borrowed)")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    yes = ap.parse_args(argv).yes
    code = clean(yes=yes)
    if not yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())
