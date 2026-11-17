"""Lakehouse first, end to end. From the recipe folder:

    uv run python rep-lakehouse-first/run.py                      # offline
    PREFIX=recipe-yourname uv run python rep-lakehouse-first/run.py --target aws
"""
from skeleton.cli import add_rep_paths, load, parser
from skeleton.config import account_id, names, session
from skeleton.events import iso
from skeleton.lake_local import LOCAL_ROOT, now
from skeleton.number import window
from skeleton.page import write_page

add_rep_paths()
import lakehouse  # noqa: E402


def main():
    args = parser("Updates -> Kinesis -> Firehose -> S3 (Parquet) -> Athena -> one number").parse_args()
    events, as_of = load(args)
    start, end = window(as_of)
    print(f"1. {len(events)} updates to send (keys: {len({e['entity_id'] for e in events})})")
    if args.target == "local":
        root = LOCAL_ROOT / "lakehouse"
        n = lakehouse.run_local(events, root, start, end, arrived=now())
        files = sorted(root.glob("updates/dt=*/*.parquet"))
        print(f"2. stream and buffer: {len(files)} Parquet files under {root}/updates/")
        print(f"3. DuckDB over the files (stands in for Athena), window {iso(start)} to {iso(end)}")
    else:
        sess = session()
        acct = account_id(sess)
        n_ = names(account=acct)
        out = lakehouse.setup(sess, n_, acct)
        print(f"2. stream {out['stream_arn']}, Firehose {n_['firehose']}, bucket {n_['bucket']}")
        sent = lakehouse.send(sess.client("kinesis"), n_["stream"], events)
        print(f"3. sent {sent} records; waiting for Firehose to write Parquet (about a minute)")
        keys = lakehouse.wait_for_lake(sess.client("s3"), n_["bucket"])
        print(f"4. {len(keys)} Parquet files under s3://{n_['bucket']}/updates/")
        n, scanned = lakehouse.number_aws(sess, n_, start, end)
        print(f"5. Athena, window {iso(start)} to {iso(end)}: Data scanned {scanned} bytes")
    page = write_page(n, "lakehouse-first", "the lake (Athena)" if args.target == "aws" else "the local lake (DuckDB)")
    print(f"updates in the last hour: {n}   (page for users: {page})")


if __name__ == "__main__":
    main()
