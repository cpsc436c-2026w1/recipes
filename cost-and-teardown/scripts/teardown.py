"""The teardown behind scripts/teardown.sh.

1. The inventory again, so a resource made after the last one is on the list.
2. Each step of scripts/teardown.toml: the steps with hourly parts first, `last = true` (the shared bucket) at the end.
3. What is still found: a ledger row recorded `created` is deleted by its kind. A row not in the ledger that carries
   the team's tag is deleted only with --tagged (the tag says the team's scripts made it, not which recipe, and
   another recipe of the team may still use it); without --tagged it fails the teardown as still there, for a
   person to look at. A `borrowed` row never is deleted, nor a row found by its name prefix alone. A kind the
   teardown does not know goes under "Delete by hand".
4. The four routes again, and each resource asked for by its own service: only its "not found" counts as gone.
   docs/resources.md is written again, each row `deleted` with the time of that answer, borrowed rows `kept`.
Exit 1 if anything is still there or left for a person, or if an inventory route was refused (the inventory is then
incomplete, so "nothing left" cannot be shown).

    bash scripts/teardown.sh     # the inventory and what each step and the leftovers would delete
    bash scripts/teardown.sh --yes            # delete; the ledger's created rows only
    bash scripts/teardown.sh --yes --tagged   # also the rows found only by the team's tag
    bash scripts/teardown.sh --yes --steps scripts/teardown-backfill.toml   # another system's steps (and its LEDGER)
"""
from __future__ import annotations

import runpy
import shlex
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
from kinds import KINDS, label  # noqa: E402
from tags import record  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STEPS = ROOT / "scripts" / "teardown.toml"


def steps(path: Path = STEPS) -> list[dict]:
    """The steps in the order they run: hourly ones first, then the others, `last` at the end."""
    all_ = tomllib.loads(path.read_text()).get("step", [])
    return ([s for s in all_ if s.get("hourly") and not s.get("last")]
            + [s for s in all_ if not s.get("hourly") and not s.get("last")] + [s for s in all_ if s.get("last")])


def run_step(step: dict, log=print) -> int:
    """A Python script runs in this process (its argv set, its exit code kept); anything else in a shell."""
    argv = shlex.split(step["command"])
    if argv[0].endswith(".py"):
        script = (ROOT / argv[0]).resolve()
        old, sys.argv = sys.argv, [str(script), *argv[1:]]
        try:
            runpy.run_path(str(script), run_name="__main__")
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:            # noqa: BLE001  a step that breaks is a failed step; the others still run
            log(f"FAILED   step {step['name']}: {type(e).__name__}: {e}")
            code = 1
        finally:
            sys.argv = old
    else:
        code = subprocess.run(step["command"], shell=True, cwd=ROOT).returncode
    log(f"step     {step['name']}: exit {code}")
    return code


def known(kind: str) -> bool:
    return kind in KINDS and KINDS[kind].delete is not None and KINDS[kind].exists is not None


def by_tag_only(r) -> bool:
    """Not in the ledger, found by the team's tag (or an IAM role's tag)."""
    return not r.in_ledger and bool({"tag", "IAM"} & set(r.found_by))


def teardown(sess=None, log=print, dry_run: bool = False, steps_file: Path = STEPS, tagged: bool = False) -> int:
    sess = sess or inventory.session()
    refused: list[str] = []
    before = inventory.take(sess, log, refused)
    inventory.write(before)
    log(f"inventory {len(before)} resources before the teardown")
    if dry_run:
        for step in steps(steps_file):
            log(f"would run {step['name']}: {step['command']}")
        for r in before:
            what = ("kept (borrowed)" if r.state == "borrowed" else "delete by hand" if not known(r.kind)
                    else "would delete" if r.in_ledger or (tagged and by_tag_only(r))
                    else "left: tag only, not in the ledger (--tagged deletes it)" if by_tag_only(r)
                    else "left: found by name only, not in the ledger")
            log(f"{what:<16} {label(r.kind)} {r.name}")
        if refused:
            log(f"INCOMPLETE inventory: route {', '.join(refused)} refused; this list may miss resources")
        return 1 if refused else 0
    for step in steps(steps_file):
        run_step(step, log)
    after = inventory.take(sess, log, refused)
    by_hand = []
    for r in after:
        if r.state == "borrowed":
            continue
        if not known(r.kind):
            by_hand.append(r)
            continue
        if r.in_ledger and r.state != "created":
            continue
        if not r.in_ledger and not (tagged and by_tag_only(r)):
            continue                      # not in the ledger: another recipe may use it; a person decides
        try:
            if KINDS[r.kind].exists(sess, r.id):
                KINDS[r.kind].delete(sess, r.id)
                log(f"removed  {label(r.kind)} {r.name} (left after the steps"
                    f"{'' if r.in_ledger else ', not in the ledger'})")
        except (ClientError, BotoCoreError) as err:
            log(f"refused  {label(r.kind)} {r.name}: "
                f"{getattr(err, 'response', {}).get('Error', {}).get('Code', type(err).__name__)}")
    final = {r.arn: r for r in before}
    for r in inventory.take(sess, log, refused):
        final.setdefault(r.arn, r)
    for r in after:
        final.setdefault(r.arn, r)
    still = []
    for r in final.values():
        if r in by_hand or any(h.arn == r.arn for h in by_hand):
            r.outcome = "delete by hand"
            continue
        if r.state == "borrowed":
            r.outcome = "kept"
            continue
        try:
            there = KINDS[r.kind].exists(sess, r.id) if r.kind in KINDS and KINDS[r.kind].exists else True
        except (ClientError, BotoCoreError):
            there = True
        if there:
            r.outcome = "still there"
            still.append(r)
            hint = " (not in the ledger: record it, delete it by hand, or run again with --tagged)" \
                if by_tag_only(r) and not tagged else ""
            log(f"FAILED: still there: {label(r.kind)} {r.name}{hint}")
        else:
            r.outcome = "deleted " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if r.in_ledger:
                record(r.kind, r.name, "deleted", arn=r.arn, script="scripts/teardown.sh")
    for r in by_hand:
        log(f"FAILED: delete by hand: {r.kind} {r.name} {r.arn}")
    rows = sorted(final.values(), key=lambda r: (not r.in_ledger, r.kind, r.name))
    routes = sorted(set(refused))
    done = not still and not by_hand and not routes
    inventory.write(rows, by_hand=by_hand, title=(
        "Torn down: every resource the team created was asked for by its own service after the deletes, and "
        "answered not found." if done else "The teardown is NOT complete: see the rows still there and "
        "Delete by hand" + (f"; inventory route {', '.join(routes)} was refused, so the list may miss resources."
                            if routes else ".")))
    if routes:
        log(f"FAILED: inventory incomplete: route {', '.join(routes)} refused; check what it lists by another "
            f"means (the console) before calling the teardown complete")
    log(f"teardown {'complete' if done else 'NOT complete'}: {len(still)} still there, {len(by_hand)} by hand"
        f"{f', {len(routes)} routes refused' if routes else ''} -> docs/resources.md")
    return 0 if done else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    ap.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    ap.add_argument("--tagged", action="store_true",
                    help="with --yes, also delete what is found only by the team's tag (not in the ledger)")
    ap.add_argument("--steps", type=Path, default=STEPS,
                    help="the steps file (default scripts/teardown.toml): one per system you tear down")
    args = ap.parse_args(argv)
    dry = args.dry_run or not args.yes
    code = teardown(dry_run=dry, steps_file=args.steps, tagged=args.tagged)
    if dry:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())
