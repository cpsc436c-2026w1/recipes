"""The team's table: a small shop's orders and stores as Parquet, made by DuckDB from a fixed recipe.

Replace this file's output with your own Parquet files (README, "Adapt it"). Same seed, same files: the tests and the
stub replies rely on the numbers below staying put.

  orders(order_id, order_date, store_id, category, quantity, amount_cad, customer_id)   1,200 rows
  stores(store_id, city, opened)                                                        4 rows
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb

from main.config import DATA_DIR

TABLES = ("orders", "stores")


def make_data(data_dir: Path = DATA_DIR) -> dict[str, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    paths = {t: data_dir / f"{t}.parquet" for t in TABLES}
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE stores AS SELECT * FROM (VALUES
            ('S1', 'Vancouver', DATE '2019-04-01'),
            ('S2', 'Burnaby',   DATE '2020-09-15'),
            ('S3', 'Richmond',  DATE '2022-03-01'),
            ('S4', 'Surrey',    DATE '2024-06-10')
        ) AS t(store_id, city, opened)
    """)
    # hash() of the row number gives the same pseudo-random columns on every machine; no random() calls.
    con.execute("""
        CREATE TABLE orders AS
        SELECT
            i AS order_id,
            DATE '2026-01-01' + CAST(i % 300 AS INTEGER) AS order_date,
            'S' || CAST(1 + hash(i * 7) % 4 AS VARCHAR) AS store_id,
            ['bread', 'pastry', 'coffee', 'cake'][1 + CAST(hash(i * 13) % 4 AS INTEGER)] AS category,
            CAST(1 + hash(i * 17) % 5 AS INTEGER) AS quantity,
            ROUND(CAST(3 + hash(i * 19) % 4000 / 100.0 AS DECIMAL(8, 2)), 2) AS amount_cad,
            'c-' || LPAD(CAST(1 + hash(i * 23) % 40 AS VARCHAR), 4, '0') AS customer_id
        FROM range(1, 1201) AS r(i)
    """)
    for t, p in paths.items():
        con.execute(f"COPY {t} TO '{p}' (FORMAT parquet)")
    con.close()
    return paths


def ensure_data(data_dir: Path = DATA_DIR) -> dict[str, Path]:
    paths = {t: data_dir / f"{t}.parquet" for t in TABLES}
    if not all(p.exists() for p in paths.values()):
        return make_data(data_dir)
    return paths


def data_version(paths: dict[str, Path]) -> str:
    """A short hash of the files' bytes. New data, new version: the answer cache keys on it (ext-answer-cache)."""
    h = hashlib.sha256()
    for name in sorted(paths):
        h.update(name.encode())
        h.update(paths[name].read_bytes())
    return h.hexdigest()[:12]
