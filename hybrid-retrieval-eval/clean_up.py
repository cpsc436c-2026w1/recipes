"""Remove everything a real run of this recipe created in your AWS account, for this PREFIX.

  PREFIX=recipe-yourname uv run python clean_up.py            list only: nothing is deleted
  PREFIX=recipe-yourname uv run python clean_up.py --yes      delete

Removes, in this order: the collection {PREFIX}-products (its index and documents go with it), the collection
group, the data access policy, the network and the encryption policies. A collection bills OCU-hours while it has
capacity, so run this when you are done, not at the end of term.

Every step is tried, also after one fails. Each line says what happened: removed, absent (it was already gone) or
FAILED with the reason. The exit code is 1 when a step failed: fix the reason and run it again.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from main.config import Settings

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


def clean_up(s: Settings, dry_run: bool = False, aoss=None, wait_s: int = 600) -> Steps:
    aoss = aoss or boto3.client("opensearchserverless", region_name=s.region)
    name = s.collection
    steps = Steps(dry_run)
    with steps.part(f"collection {name}"):
        found = aoss.batch_get_collection(names=[name]).get("collectionDetails", [])
        if not found:
            steps.absent.append(f"collection {name}")
        elif found[0].get("status") == "DELETING":       # an earlier clean-up asked for it: wait it out
            steps.absent.append(f"collection {name} (already being deleted)")
            if not dry_run:
                _wait_gone(aoss, name, wait_s)
        elif steps.do(f"collection {name}", aoss.delete_collection, id=found[0]["id"]) and not dry_run:
            _wait_gone(aoss, name, wait_s)
    with steps.part(f"collection group {name}"):
        groups = aoss.batch_get_collection_group(names=[name]).get("collectionGroupDetails", [])
        if not groups:
            steps.absent.append(f"collection group {name}")
        else:
            if steps.do(f"collection group {name}", aoss.delete_collection_group, id=groups[0]["id"]) \
                    and not dry_run:
                # Wait until it is gone, so a run right after this clean-up does not find it half deleted.
                _wait_group_gone(aoss, name, wait_s)
    # The policies are looked up by name, not in a list: a list call returns one page, and in an account that
    # teammates share the policy can be on a later page.
    with steps.part(f"data access policy {name}"):
        aoss.get_access_policy(name=name, type="data")
        steps.do(f"data access policy {name}", aoss.delete_access_policy, name=name, type="data")
    for kind in ("network", "encryption"):
        with steps.part(f"{kind} policy {name}"):
            aoss.get_security_policy(name=name, type=kind)
            steps.do(f"{kind} policy {name}", aoss.delete_security_policy, name=name, type=kind)
    return steps


def _wait_gone(aoss, name: str, wait_s: int) -> None:
    """The group and the encryption policy cannot go while the collection still exists."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not aoss.batch_get_collection(names=[name]).get("collectionDetails"):
            return
        time.sleep(10)
    raise TimeoutError(f"collection {name} still there after {wait_s} s; run clean_up.py again")


def _wait_group_gone(aoss, name: str, wait_s: int) -> None:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not aoss.batch_get_collection_group(names=[name]).get("collectionGroupDetails"):
            return
        time.sleep(5)
    raise TimeoutError(f"collection group {name} still there after {wait_s} s; run clean_up.py again")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    args = p.parse_args(argv)
    args.dry_run = args.dry_run or not args.yes
    s = Settings()
    steps = clean_up(s, args.dry_run)
    code = steps.report()
    verb = "would be removed" if args.dry_run else "removed"
    print(f"{len(steps.removed)} resources {verb}, {len(steps.failed)} failed, for PREFIX={s.prefix} in {s.region}")
    if code:
        print("Something is left: read the FAILED lines, fix the reason, and run clean_up.py again.")
    if args.dry_run:
        print("Nothing deleted. Run again with --yes to delete these.")
    sys.exit(code)


if __name__ == "__main__":
    main()
