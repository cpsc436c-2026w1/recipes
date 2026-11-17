"""The two read-only layers, without the server: the statement check and the locked engine."""

from __future__ import annotations

import pytest

import duckdb

from main.engine import QueryRefused, check_select


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM orders",
    "  select * from stores;  ",
    "WITH big AS (SELECT * FROM orders WHERE amount_cad > 100) SELECT count(*) FROM big",
    "SELECT 'DELETE FROM orders' AS just_text",   # the word in a string is not a statement
])
def test_select_passes(sql):
    assert check_select(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET amount_cad = 0",
    "INSERT INTO stores VALUES (9, 'x', 'y', DATE '2026-01-01')",
    "DROP TABLE orders",
    "CREATE TABLE copy AS SELECT * FROM orders",
    "COPY orders TO 'out.csv'",
    "ATTACH 'other.duckdb'",
    "SET enable_external_access = true",
    "PRAGMA version",
    "INSTALL httpfs",
    "SELECT 1; DELETE FROM orders",
    "SELECT 1; SELECT 2",
    "",
    "SELEC oops",
])
def test_everything_else_is_refused(sql):
    with pytest.raises(QueryRefused):
        check_select(sql)


def test_engine_answers_and_caps_rows(engine):
    r = engine.query("SELECT * FROM orders")
    assert len(r.rows) == 50 and r.truncated            # 200 rows match, 50 come back
    r = engine.query("SELECT * FROM stores")
    assert len(r.rows) == 6 and not r.truncated


def test_engine_cannot_read_files_even_in_a_select(engine):
    with pytest.raises(Exception, match="(?i)permission|disabled"):
        engine.query("SELECT * FROM read_csv('/etc/hosts')")


def test_schema_lists_tables_and_columns(engine):
    schema = {t["table"]: t for t in engine.schema()}
    assert set(schema) == {"orders", "stores"}
    assert schema["orders"]["rows"] == 200
    assert {c["name"] for c in schema["stores"]["columns"]} == {"store_id", "store_name", "city", "opened"}


@pytest.mark.parametrize("sql", [
    "WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d",
    "WITH d AS (UPDATE orders SET amount_cad = 0 RETURNING *) SELECT count(*) FROM d",
    "WITH d AS (INSERT INTO stores VALUES (9, 'x', 'y', DATE '2026-01-01') RETURNING *) SELECT * FROM d",
])
def test_a_write_inside_a_select_changes_nothing(engine, sql):
    # DuckDB's parser types these as SELECT, so check_select passes them; the read-only transaction refuses them.
    before = engine.query("SELECT (SELECT count(*) FROM orders), (SELECT sum(amount_cad) FROM orders), "
                          "(SELECT count(*) FROM stores)").rows
    assert check_select(sql)
    with pytest.raises(duckdb.Error, match="read-only"):
        engine.query(sql)
    with pytest.raises(duckdb.Error, match="read-only"):
        engine.fetch_one(sql, [])
    after = engine.query("SELECT (SELECT count(*) FROM orders), (SELECT sum(amount_cad) FROM orders), "
                         "(SELECT count(*) FROM stores)").rows
    assert after == before == [[200, before[0][1], 6]]


def test_a_failed_query_leaves_the_engine_usable(engine):
    with pytest.raises(duckdb.Error):
        engine.query("SELECT no_such_column FROM orders")
    assert engine.query("SELECT count(*) FROM orders").rows == [[200]]
    assert engine.fetch_one("SELECT amount_cad FROM orders WHERE order_id = ?", [1042]) is not None
