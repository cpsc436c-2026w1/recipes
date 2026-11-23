"""SIM01 "two days", the offline DS01: five rows with DS01's columns, 3 on 2024-03-14 (2 in zone 161, 1 in zone 230)
and 2 on 2024-03-15 (zone 230), as one local Parquet file."""

from __future__ import annotations

from pathlib import Path

import duckdb


def write_sim01(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.execute(f"""COPY (SELECT * FROM (VALUES
        (TIMESTAMP '2024-03-14 08:01:00', 161, 230, 1.2, 540, 12.5, 9.8, DATE '2024-03-14'),
        (TIMESTAMP '2024-03-14 17:40:00', 161, 48, 2.9, 1260, 21.0, 16.4, DATE '2024-03-14'),
        (TIMESTAMP '2024-03-14 22:05:00', 230, 161, 0.9, 420, 9.75, 7.1, DATE '2024-03-14'),
        (TIMESTAMP '2024-03-15 07:30:00', 230, 237, 3.4, 1500, 24.1, 19.0, DATE '2024-03-15'),
        (TIMESTAMP '2024-03-15 19:12:00', 230, 161, 1.1, 610, 11.3, 8.6, DATE '2024-03-15'))
        t(pickup_time, pickup_zone_id, dropoff_zone_id, trip_miles, trip_time_s, fare, driver_pay, dt))
        TO '{path}' (FORMAT parquet)""")
    return path
