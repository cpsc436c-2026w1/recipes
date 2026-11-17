"""Remove every table a real run of this recipe created, for this PREFIX.

  PREFIX=recipe-<you> uv run python clean_up.py --dry-run   list only
  PREFIX=recipe-<you> uv run python clean_up.py             delete

Removes the DynamoDB tables {PREFIX}-chat-conversations, -chat-history, -answer-cache and -llm-traces.
The recipe creates nothing else. Every table is tried, also after one fails; a failure is printed and the exit
code is 1.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent / "main"))

from config import Settings  # noqa: E402

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


def clean_up(s: Settings, ddb, dry_run: bool = False) -> Steps:
    steps = Steps(dry_run)
    for name in s.all_tables():
        what = f"dynamodb table {name}"
        with steps.part(what):
            status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
            if status == "DELETING":
                # An earlier clean-up already asked for it. A second DeleteTable returns no error, so the call is
                # left out and the table is reported as absent, not as removed twice.
                # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html (read 2026-11-13)
                steps.absent.append(f"{what} (already being deleted)")
            else:
                steps.do(what, ddb.delete_table, TableName=name)
    return steps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    s = Settings()
    steps = clean_up(s, boto3.client("dynamodb", region_name=s.region), args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    for what in steps.removed:
        print(f"{verb} {what}")
    for what in steps.absent:
        print(f"absent {what}")
    print(f"{len(steps.removed)} tables {'to remove' if args.dry_run else 'removed'} for PREFIX={s.prefix} in {s.region}")
    if steps.failed:
        for line in steps.failed:
            print(f"FAILED {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
