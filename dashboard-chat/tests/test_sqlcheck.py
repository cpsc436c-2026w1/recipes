import pytest

from main.sqlcheck import SqlRejected, check_sql

TABLES = ("orders", "stores")


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT city, SUM(amount_cad) FROM orders JOIN stores USING (store_id) GROUP BY city;",
    "WITH t AS (SELECT * FROM orders) SELECT COUNT(*) FROM t",
    "SELECT category FROM orders UNION SELECT city FROM stores",
    "SELECT updated_at FROM (SELECT order_date AS updated_at FROM orders)",
])
def test_reads_pass(sql):
    check_sql(sql, TABLES)


@pytest.mark.parametrize("sql,why", [
    ("DELETE FROM orders", "SELECT"),
    ("DROP TABLE orders", "SELECT"),
    ("SELECT 1; DROP TABLE orders", "exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
    ("SELECT * FROM '/etc/passwd'", "unknown table"),
    ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "read_parquet"),
    ("SELECT getenv('HOME')", "getenv"),
    ("SELECT * FROM customers", "unknown table"),
    ("SELECT * FROM main.orders", "plain table names"),
    ("COPY orders TO 'out.csv'", "SELECT"),
    ("SELEC nonsense", "SELECT"),
    ("SELECT FROM WHERE (", "parse"),
])
def test_writes_files_and_unknown_tables_are_rejected(sql, why):
    with pytest.raises(SqlRejected, match=why):
        check_sql(sql, TABLES)


def test_engine_blocks_files_even_without_the_check(engine):
    # the second layer: the engine itself refuses to read a file
    with pytest.raises(Exception):
        engine.run("SELECT * FROM read_csv('/etc/hostname')")
    with pytest.raises(Exception):
        engine.run("SET enable_external_access = true")


def test_engine_cuts_rows_and_long_text_and_says_which(engine):
    assert engine.run("SELECT COUNT(*) FROM orders")["truncated"] is None
    many = engine.run("SELECT order_id FROM orders")
    assert len(many["rows"]) == 50 and many["truncated"] == "rows"
    long_text = engine.run("SELECT repeat('x', 500) AS note, 1 AS n")
    assert len(long_text["rows"][0][0]) == 200 and long_text["rows"][0][1] == 1 and long_text["truncated"] == "text"
    assert engine.run("SELECT repeat('x', 201) FROM orders")["truncated"] == "rows and text"
    assert engine.run("SELECT repeat('x', 200) AS note")["truncated"] is None  # 200 characters fit


def test_engine_stops_a_query_at_its_time_limit(paths):
    import time

    import duckdb

    from main.engine import QUERY_TIMEOUT_S, Engine

    assert QUERY_TIMEOUT_S == 5 and Engine(paths).query_timeout_s == 5  # the number in the README
    engine, start = Engine(paths, query_timeout_s=0.2), time.monotonic()
    with pytest.raises(duckdb.Error, match="query stopped after 0.2 s"):
        engine.run("SELECT COUNT(*) FROM orders a, orders b, orders c, orders d")
    assert time.monotonic() - start < 5
    assert engine.run("SELECT COUNT(*) FROM orders")["rows"] == [[1200]]  # the engine still answers


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "DROP TABLE orders", "UPDATE orders SET quantity = 0",
                                 "ROLLBACK; DELETE FROM orders", "SELECT 1; DELETE FROM orders"])
def test_engine_refuses_a_write_even_without_the_check(engine, sql):
    import duckdb

    with pytest.raises(duckdb.Error, match="read-only|one statement"):
        engine.run(sql)
    assert engine.run("SELECT COUNT(*) FROM orders")["rows"] == [[1200]]
