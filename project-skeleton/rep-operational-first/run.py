"""Operational first, end to end. From the recipe folder:

    uv run python rep-operational-first/run.py                    # offline
    PREFIX=recipe-yourname uv run python rep-operational-first/run.py --target aws
"""
from skeleton.cli import add_rep_paths, load, parser
from skeleton.config import account_id, names, prefix, session
from skeleton.events import iso
from skeleton.lake_local import LOCAL_ROOT
from skeleton.number import window
from skeleton.offline import fake_aws
from skeleton.page import write_page

add_rep_paths()
import operational  # noqa: E402


def main():
    p = parser("Updates -> API (Lambda function URL) -> DynamoDB -> export to S3 -> Athena")
    p.add_argument("--skip-export", action="store_true", help="aws: stop after the API answers (the export takes minutes)")
    args = p.parse_args()
    events, as_of = load(args)
    start, end = window(as_of)
    print(f"1. {len(events)} updates to send (keys: {len({e['entity_id'] for e in events})})")
    if args.target == "local":
        with fake_aws():
            r = operational.run_local(events, LOCAL_ROOT / "operational", as_of, table=f"{prefix()}-updates")
        s = r["sent"]
        print(f"2. POST /updates: stored {s['stored']}, older {s['older']}, duplicates {s['duplicates']}")
        print(f"3. GET /number, window {r['api']['window_start']} to {r['api']['window_end']}: "
              f"{r['api']['updates_last_hour']}")
        print(f"4. export of {r['items']} items to {r['export_folder']}; DuckDB stands in for Athena")
        page = write_page(r["api"]["updates_last_hour"], "operational-first", "GET /number (local)")
        print(f"updates in the last hour: API {r['api']['updates_last_hour']}, lake {r['lake']}   "
              f"(page for users: {page})")
        return
    sess = session()
    acct = account_id(sess)
    n = names(account=acct)
    url = operational.setup(sess, n, acct)
    print(f"2. function URL {url} (AWS_IAM: requests are signed)")
    s = operational.send(sess, url, events)
    print(f"3. POST /updates: stored {s['stored']}, older {s['older']}, duplicates {s['duplicates']}")
    api = operational.call(sess, url, "GET", "/number", params={"as_of": iso(as_of)})
    print(f"4. GET /number, window {api['window_start']} to {api['window_end']}: {api['updates_last_hour']}")
    print(f"   page for users: {write_page(api['updates_last_hour'], 'operational-first', 'GET /number')}")
    if args.skip_export:
        print(f"updates in the last hour: API {api['updates_last_hour']}")
        return
    print("5. exporting the table to S3 (several minutes)")
    folder = operational.export(sess, n, acct)
    operational.point_lake_at(sess, n, folder)
    lake, scanned = operational.number_lake_aws(sess, n, start, end)
    print(f"6. Athena over {folder}: Data scanned {scanned} bytes")
    print(f"updates in the last hour: API {api['updates_last_hour']}, lake {lake}")


if __name__ == "__main__":
    main()
