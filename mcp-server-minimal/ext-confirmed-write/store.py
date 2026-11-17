"""Where approved refunds are written: one DuckDB file, one row per request id (ext README, "Decisions").

The request id is the primary key, so a retried call with the same id cannot add a second row. In DynamoDB the
same rule is a conditional put with `attribute_not_exists(request_id)`: see ../../concurrency-correctness/ext-idempotent-retry.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from confirm import Refund


class RefundStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS refunds (
                request_id VARCHAR PRIMARY KEY,
                order_id INTEGER NOT NULL,
                amount_cad DECIMAL(10, 2) NOT NULL,
                reason VARCHAR NOT NULL,
                approved_by VARCHAR NOT NULL,
                applied_at VARCHAR NOT NULL         -- UTC, ISO 8601
            )""")
        self._lock = threading.Lock()

    def apply(self, refund: Refund, approver: str) -> dict:
        """Write the refund once. Returns status applied, already_applied (same change, nothing written again)
        or conflict (the request id was used for a different change, nothing written)."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            inserted = self.con.execute(
                "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING request_id",
                [refund.request_id, refund.order_id, refund.amount_cad, refund.reason, approver, now]).fetchall()
            if inserted:
                return {"status": "applied", "request_id": refund.request_id, "approved_by": approver,
                        "applied_at": now}
            order_id, amount, reason, by, at = self.con.execute(
                "SELECT order_id, amount_cad, reason, approved_by, applied_at FROM refunds WHERE request_id = ?",
                [refund.request_id]).fetchone()
        same = (order_id, f"{float(amount):.2f}", reason) == (refund.order_id, f"{refund.amount_cad:.2f}",
                                                              refund.reason)
        return {"status": "already_applied" if same else "conflict", "request_id": refund.request_id,
                "approved_by": by, "applied_at": at}

    def count(self) -> int:
        with self._lock:
            return self.con.execute("SELECT count(*) FROM refunds").fetchone()[0]

    def close(self) -> None:
        self.con.close()
