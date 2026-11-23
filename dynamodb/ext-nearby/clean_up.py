#!/usr/bin/env python3
"""Clean-up of ext-nearby: delete what its ledger says it created, and remove its attributes from <PREFIX>-drivers.

    python3 ext-nearby/clean_up.py           dry run: print what would be deleted, delete nothing
    python3 ext-nearby/clean_up.py --yes     delete it, then check that none of it is left

Only tables of this PREFIX that the ledger (state/ledger.jsonl, or $LEDGER) records as `created` are deleted. The
table <PREFIX>-drivers is borrowed from ext-riders-and-drivers: it is never deleted here; its SIM02 drivers lose the
attributes cell, car_type, lat, lon and position_time, and a driver item that held nothing else is deleted. The
whole recipe's `bash clean_up.sh` (it lists what it would delete), then `bash clean_up.sh --yes` runs this file first, then deletes <PREFIX>-drivers with the other tables.
Every step is attempted; a refused one prints FAILED and the exit code is 1. "Not found" means already gone.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))
import nearby  # noqa: E402
from tags import ledger_rows, prefix, record  # noqa: E402

ADDED = ["cell", "car_type", "lat", "lon", "position_time"]


def last_states() -> dict[str, str]:
    """The last ledger state of each table of this PREFIX."""
    last = {}
    for r in ledger_rows():
        if r["type"] == "dynamodb-table" and r.get("prefix") == prefix():
            last[r["name"]] = r["state"]
    return last


def owned_tables() -> list[str]:
    """This extension's two tables, when their last ledger line is `created`. Only these two names: a ledger shared
    with another recipe names its tables too, and those are never deleted here."""
    last = last_states()
    mine = (nearby.names()["cells"], nearby.names()["points"])
    return [t for t in mine if last.get(t) == "created"]


def borrowed_drivers() -> bool:
    """Strip the drivers only while the cell table is still recorded `created`: after a clean-up has run, a second
    one makes no writes."""
    last = last_states()
    return last.get(nearby.names()["drivers"]) == "borrowed" and last.get(nearby.names()["cells"]) == "created"


def driver_ids(extra: list[str] = ()) -> list[str]:
    return [d["driver_id"] for d in nearby.sim_drivers()] + list(extra)


def strip_driver(c, table: str, driver_id: str) -> str:
    """Remove the added attributes from one driver; delete the item when only its key is left. Returns what it did."""
    names = {f"#a{i}": a for i, a in enumerate(ADDED)}
    try:
        reply = c.update_item(TableName=table, Key={"driver_id": {"S": driver_id}},
                              UpdateExpression="REMOVE " + ", ".join(names),
                              ConditionExpression="attribute_exists(driver_id)", ExpressionAttributeNames=names,
                              ReturnValues="ALL_NEW")
    except c.exceptions.ConditionalCheckFailedException:
        return "absent"
    if set(reply.get("Attributes", {})) <= {"driver_id"}:
        try:
            c.delete_item(TableName=table, Key={"driver_id": {"S": driver_id}},
                          ConditionExpression="attribute_exists(driver_id) AND attribute_not_exists(#c)",
                          ExpressionAttributeNames={"#c": "cell"})
        except c.exceptions.ConditionalCheckFailedException:
            return "kept (moved again meanwhile)"
        return "deleted"
    return "stripped"


def main(argv=None, c=None, extra_drivers: list[str] = ()) -> int:
    argv = sys.argv[1:] if argv is None else argv
    yes = "--yes" in argv
    owned = owned_tables()
    drivers = nearby.names()["drivers"]
    strip = borrowed_drivers()
    print(f"ext-nearby clean-up, PREFIX={prefix()}" + ("" if yes else " (dry run: add --yes to delete)"))
    print("tables to delete (created by this extension, from the ledger): " + (", ".join(owned) or "none"))
    if strip:
        print(f"table {drivers}: borrowed, kept; attributes {', '.join(ADDED)} removed from the SIM02 drivers")
    if not yes:
        print("Nothing deleted. Run again with --yes to delete these.")
        return 0
    c = c or nearby.client()
    failed = 0
    if strip:
        try:
            if nearby.table_status(c, drivers) is None:
                print(f"(table {drivers} not found: already gone)")
            else:
                done: dict[str, int] = {}
                for d in driver_ids(extra_drivers):
                    what = strip_driver(c, drivers, d)
                    done[what] = done.get(what, 0) + 1
                print(f"{drivers}: " + ", ".join(f"{n} {w}" for w, n in sorted(done.items())))
        except Exception as e:  # noqa: BLE001  every step is attempted
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            print(f"FAILED: the attributes of {drivers} were not all removed", file=sys.stderr)
            failed = 1
    for t in owned:
        try:
            status = nearby.table_status(c, t)
            if status is None:
                print(f"(table {t} not found: already gone)")
            else:
                if status != "DELETING":
                    c.delete_table(TableName=t)
                    print(f"deleting table {t}")
                else:
                    print(f"(table {t} is already being deleted: waiting)")
                c.get_waiter("table_not_exists").wait(TableName=t)
            record("dynamodb-table", t, "deleted", script="ext-nearby/clean_up.py --yes")
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            print(f"FAILED: table {t} was not deleted", file=sys.stderr)
            failed = 1
    # last check: none of this extension's two table names is left
    for t in (nearby.names()["cells"], nearby.names()["points"]):
        try:
            if nearby.table_status(c, t) is None:
                print(f"(no table {t} left)")
            elif t in owned:
                print(f"FAILED: still there: table {t}", file=sys.stderr)
                failed = 1
            else:
                print(f"(table {t} is there but not in this ledger as created: kept)")
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            print(f"FAILED: could not check for table {t}", file=sys.stderr)
            failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main())
