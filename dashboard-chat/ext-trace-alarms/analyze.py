"""Read the spans into DuckDB and answer the questions you ask when the chat is slow, wrong or expensive.

  uv run python ext-trace-alarms/analyze.py                 output/spans.jsonl (from trace_run.py --target local)
  uv run python ext-trace-alarms/analyze.py --target aws    every item of {PREFIX}-chat-spans (a Scan: fine for a
                                                            demo's few hundred items, not for a big table)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main.config import OUTPUT_DIR, Settings  # noqa: E402

QUERIES = {
    "per request": """
        SELECT count(*) AS requests,
               round(median(duration_ms)) AS median_ms,
               round(quantile_cont(duration_ms, 0.9)) AS p90_ms,
               sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
               round(sum(cost_cad), 6) AS cost_cad
        FROM spans WHERE kind = 'request'""",
    "time by kind of call": """
        SELECT kind, count(*) AS calls, round(sum(duration_ms)) AS total_ms,
               round(100 * sum(duration_ms) / (SELECT sum(duration_ms) FROM spans WHERE kind <> 'request'), 1) AS pct
        FROM spans WHERE kind <> 'request' GROUP BY kind ORDER BY total_ms DESC""",
    "calls per request": """
        SELECT n_calls, count(*) AS requests FROM (
            SELECT trace_id, count(*) AS n_calls FROM spans WHERE kind <> 'request' GROUP BY trace_id)
        GROUP BY n_calls ORDER BY n_calls""",
    "slowest requests, with their calls": """
        SELECT r.question, r.duration_ms AS request_ms, c.span_id, c.kind, c.name, c.duration_ms, c.status
        FROM (SELECT * FROM spans WHERE kind = 'request' ORDER BY duration_ms DESC LIMIT 3) r
        JOIN spans c ON c.trace_id = r.trace_id AND c.parent_id = r.span_id
        ORDER BY r.duration_ms DESC, c.span_id""",
}


def load(con, items: list[dict] | None, path: Path | None) -> None:
    if items is not None:  # DynamoDB items: Decimal to float, then through a temporary JSON Lines file
        tmp = Path(tempfile.mkdtemp()) / "spans.jsonl"
        tmp.write_text("".join(json.dumps(i, default=float) + "\n" for i in items))
        path = tmp
    con.execute("CREATE OR REPLACE TABLE spans AS SELECT * FROM read_json_auto(?)", [str(path)])


def report(con) -> dict[str, list]:
    return {title: con.execute(sql).fetchall() for title, sql in QUERIES.items()}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    con = duckdb.connect()
    if args.target == "aws":
        from spans import DynamoSpans

        s = Settings()
        load(con, DynamoSpans(s.spans_table, s.region).scan_all(), None)
    else:
        load(con, None, OUTPUT_DIR / "spans.jsonl")
    for title, sql in QUERIES.items():
        print(f"\n{title}")
        print(con.sql(sql))


if __name__ == "__main__":
    main()
