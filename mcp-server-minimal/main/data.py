"""The team's data: a small shop's stores and orders, written once as Parquet.

Replace this file with your own tables (README, "Adapt it"). The server reads every name listed in TABLES from
{DATA_DIR}/{name}.parquet; nothing else in the recipe knows the column names.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# name -> the query that makes the sample table. Values are computed, not random, so every run has the same rows.
TABLES = {
    "stores": """
        SELECT * FROM (VALUES
            (1, 'Kitsilano', 'Vancouver', DATE '2019-04-02'),
            (2, 'Commercial Drive', 'Vancouver', DATE '2021-06-15'),
            (3, 'Lonsdale', 'North Vancouver', DATE '2020-09-01'),
            (4, 'Metrotown', 'Burnaby', DATE '2022-03-20'),
            (5, 'Fort Street', 'Victoria', DATE '2018-11-11'),
            (6, 'Bernard Avenue', 'Kelowna', DATE '2024-05-04')
        ) AS t(store_id, store_name, city, opened)
    """,
    "orders": """
        SELECT
            i AS order_id,
            'c' || lpad(((i * 37) % 40)::VARCHAR, 3, '0') AS customer_id,
            (i % 6) + 1 AS store_id,
            DATE '2026-09-01' + ((i * 11) % 60)::INTEGER AS order_date,
            round(5 + ((i * 7919) % 20000) / 100.0, 2) AS amount_cad
        FROM range(1000, 1200) AS t(i)
    """,
}


def ensure_data(data_dir: Path) -> dict[str, Path]:
    """Write the sample Parquet files if they are missing; return name -> path."""
    data_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: data_dir / f"{name}.parquet" for name in TABLES}
    con = duckdb.connect()
    for name, sql in TABLES.items():
        if not paths[name].exists():
            con.execute(f"COPY ({sql}) TO '{paths[name]}' (FORMAT parquet)")
    con.close()
    return paths
