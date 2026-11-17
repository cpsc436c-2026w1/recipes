"""The same question asked of the lake: how many updates arrived in the last hour, over the table's export.

No model here: one fixed SELECT over the view `events` (Athena in AWS, DuckDB offline), then the table's own count
for the same window. The two agree when the export is newer than the last update; the lake is as fresh as its export
(README, Decisions: "where the assistant reads").

    uv run python rep-genai-first/lake_example.py                                   # offline
    PREFIX=recipe-yourname uv run python rep-genai-first/lake_example.py --target aws  # after operational first's export

For a model that writes its own SQL over the lake, see the dashboard-chat recipe.
"""
from datetime import datetime

from skeleton.aws import athena_query
from skeleton.cli import add_rep_paths, load, parser
from skeleton.config import account_id, names, prefix, session
from skeleton.events import iso
from skeleton.lake_local import LOCAL_ROOT, query
from skeleton.number import EVENTS_VIEW_SQL, window
from skeleton.offline import fake_aws

add_rep_paths()
import genai  # noqa: E402
import operational  # noqa: E402


def events_sql(start: datetime, end: datetime) -> str:
    """The question as SQL over the view `events` (plain columns over the export; one row per stored update)."""
    return (f"SELECT count(*) AS updates_last_hour FROM events "
            f"WHERE event_time >= '{iso(start)}' AND event_time < '{iso(end)}'")


def main():
    args = parser("The same question over the lake: Athena (DuckDB offline) over the DynamoDB export").parse_args()
    events, as_of = load(args)
    start, end = window(as_of)
    sql = events_sql(start, end)
    if args.target == "local":
        root = LOCAL_ROOT / "genai-lake"
        with fake_aws():
            built = operational.run_local(events, root, as_of, table=f"{prefix()}-updates")
        print(f"1. table and its export built offline: export in {built['export_folder']}")
        lake = int(query(root, sql)[0]["updates_last_hour"])
        print(f"2. DuckDB (stands in for Athena): {sql}")
        table = built["api"]["updates_last_hour"]
    else:
        sess = session()
        n = names(account=account_id(sess))
        athena = sess.client("athena")
        athena_query(athena, f"CREATE OR REPLACE VIEW events AS {EVENTS_VIEW_SQL}", n["workgroup"], n["database"])
        print(f"1. view events over the newest export in {n['database']} (run rep-operational-first first)")
        rows, scanned = athena_query(athena, sql, n["workgroup"], n["database"])
        lake = int(rows[0]["updates_last_hour"])
        print(f"2. Athena: {sql}\n   Data scanned {scanned} bytes")
        table = genai.count_updates(sess.client("dynamodb"), n["table"], iso(start), iso(end))["updates"]
    print(f"updates in the last hour: lake {lake}, table {table}")


if __name__ == "__main__":
    main()
