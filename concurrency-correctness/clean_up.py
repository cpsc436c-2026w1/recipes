"""Remove everything this recipe creates: the tables $PREFIX-race-trips and $PREFIX-race-requests.

    PREFIX=recipe-ana uv run python clean_up.py

Only these exact names are touched. Safe to run twice. Both tables are attempted, also when the first is refused;
a refused step is printed as FAILED and the exit code is then 1.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path[:0] = [str(Path(__file__).parent / "main"), str(Path(__file__).parent / "ext-idempotent-retry")]
import tips  # noqa: E402
import trips  # noqa: E402

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


def delete_table(ddb, name: str, poll_s: float = 2.0) -> None:
    """Delete the table and wait until it is gone, so that a run right after the clean-up can create it again."""
    ddb.delete_table(TableName=name)
    ddb.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})


def clean(session, prefix: str) -> Steps:
    ddb = session.client("dynamodb")
    steps = Steps()
    for name in (trips.table_name(prefix), tips.requests_table_name(prefix)):
        with steps.part(f"table {name}"):
            if ddb.describe_table(TableName=name)["Table"]["TableStatus"] == "DELETING":
                # an earlier clean-up asked for it (DynamoDB would accept a second delete without an error): wait
                ddb.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 150})
                steps.absent.append(f"table {name} (was already being deleted)")
            else:
                steps.do(f"table {name}", delete_table, ddb=ddb, name=name)
    if steps.report():
        print("the clean-up is NOT complete: fix what FAILED says, then run clean_up.py again")
    return steps


if __name__ == "__main__":
    prefix = os.environ.get("PREFIX", "recipe")
    region = os.environ.get("AWS_REGION", "ca-central-1")
    print(f"removing the concurrency-correctness tables with prefix {prefix!r} in {region}")
    sys.exit(1 if clean(boto3.Session(region_name=region), prefix).failed else 0)
