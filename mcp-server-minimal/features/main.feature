Feature: An MCP server over the team's tables, the main scenario
  Two read tools behind a bearer token: get_schema lists the tables, query runs one read-only SELECT.
  README, "Requirements": R10 to R19, R20 to R24, R40.

  Background:
    Given the main server is running with the sample tables

  @R10
  Scenario: The tools a client is offered
    When a client lists the tools
    Then the tools are "get_schema, query"
    And every tool is marked read-only

  @R11
  Scenario: The schema of the team's tables
    When a client calls get_schema
    Then the tool result is not an error
    And the schema lists the table "orders" with 200 rows and the columns "order_id, customer_id, store_id, order_date, amount_cad"
    And the schema lists the table "stores" with 6 rows and the columns "store_id, store_name, city, opened"
    And every column in the schema has a name and a type

  @R12
  Scenario: One SELECT
    When a client calls query with "SELECT city, count(*) AS n FROM stores GROUP BY city ORDER BY n DESC, city LIMIT 2"
    Then the tool result is not an error
    And the result's columns are "city, n"
    And the result's rows are [["Vancouver", 2], ["Burnaby", 1]]
    And the result says it was not truncated

  @R13 @R14
  Scenario: A query that matches more rows than the cap
    When a client calls query with "SELECT order_id FROM orders ORDER BY order_id"
    Then the result has 50 rows
    And the result says it was truncated

  @R13 @R14
  Scenario: A query that matches exactly as many rows as the cap
    When a client calls query with "SELECT order_id FROM orders ORDER BY order_id LIMIT 50"
    Then the result has 50 rows
    And the result says it was not truncated

  @R13
  Scenario: MAX_ROWS names another cap
    Given MAX_ROWS is "7" and the main server is started from the environment
    When a client calls query with "SELECT order_id FROM orders ORDER BY order_id"
    Then the result has 7 rows
    And the result says it was truncated

  @R15 @R16
  Scenario: A text value longer than the limit is cut, and the result says so
    When a client calls query with "SELECT repeat('x', 500) AS note, 7 AS n"
    Then the first value of the result's first row has 200 characters
    And the result says a text value was cut
    And the last log line says a text value was cut

  @R15 @R16
  Scenario: A text value of exactly the limit is whole
    When a client calls query with "SELECT repeat('x', 200) AS note"
    Then the first value of the result's first row has 200 characters
    And the result says no text value was cut

  @R17 @R14
  Scenario: A result that is too large as a whole loses rows from its end, and says so
    When a client calls query with "SELECT order_id, repeat('a', 200) AS a, repeat('b', 200) AS b, repeat('c', 200) AS c FROM orders ORDER BY order_id LIMIT 50"
    Then the result has fewer than 50 rows and more than 20
    And the rows of the result are at most 20000 characters as JSON
    And the result's first row is for order 1000
    And the result says it was truncated
    And the last log line says the result was truncated

  @R17
  Scenario: A result under the size limit keeps all its rows
    When a client calls query with "SELECT order_id, repeat('a', 200) AS a, repeat('b', 200) AS b, repeat('c', 200) AS c FROM orders ORDER BY order_id LIMIT 20"
    Then the result has 20 rows
    And the result says it was not truncated

  @R18
  Scenario: A result with more than 100 columns keeps the first 100, and says so
    When a client calls query with a SELECT of 150 columns named c1 to c150 from one order
    Then the result has 1 rows
    And the result's columns are c1 to c100
    And the result says its columns were cut
    And the last log line says the columns were cut

  @R18
  Scenario: A result of 100 columns keeps them all
    When a client calls query with a SELECT of 100 columns named c1 to c100 from one order
    Then the result's columns are c1 to c100
    And the result says its columns were not cut

  @R19
  Scenario: A first row that is too large alone gives no row and a note, not an empty result that looks whole
    When a client calls query with a SELECT of 100 columns of 200 characters from every order
    Then the result has 0 rows
    And the result says it was truncated
    And the result's note is "the first row alone is over 20,000 characters as JSON, so no row is returned: select fewer columns"
    And the last log line says the result was truncated
    And the last log line has the result's note

  @R19
  Scenario: A result that fits has no note
    When a client calls query with "SELECT order_id FROM orders LIMIT 3"
    Then the result has no note

  @R24
  Scenario: The engine's memory limit is set and locked
    When a client calls query with "SELECT current_setting('memory_limit') AS memory_limit"
    Then the result's rows are [["953.6 MiB"]]
    When a client calls query with "SET memory_limit = '64GB'"
    Then the tool result is an error that holds "query refused: only SELECT is allowed"

  @R24
  Scenario: The engine's settings are locked for the server's own code too
    Then the engine refuses "SET enable_external_access = true" with an error that holds "configuration has been locked"
    And the engine refuses "SET memory_limit = '64GB'" with an error that holds "configuration has been locked"

  @R20 @R40
  Scenario Outline: Text that is not one SELECT is refused and not run
    When a client calls query with "<sql>"
    Then the tool result is an error that holds "query refused: <reason>"
    And the last log line has tool "query" and status "refused"
    And the last log line gives the reason "<kind>" and not the SQL
    And the table "orders" still has 200 rows

    Examples:
      | sql                                      | reason                   | kind               |
      | DELETE FROM orders                       | only SELECT is allowed   | DELETE             |
      | UPDATE orders SET amount_cad = 0         | only SELECT is allowed   | UPDATE             |
      | DROP TABLE orders                        | only SELECT is allowed   | DROP               |
      | SELECT 1; DELETE FROM orders             | send one statement       | several_statements |
      | COPY orders TO 'out.csv'                 | only SELECT is allowed   | COPY               |
      | SET enable_external_access = true        | only SELECT is allowed   | SET                |
      | SELEC city FROM stores                   | the query does not parse | does_not_parse     |

  @R20
  Scenario: An empty query is refused
    When a client calls query with nothing
    Then the tool result is an error that holds "query refused: empty query"
    And the last log line gives the reason "empty" and not the SQL

  @R21
  Scenario Outline: A write inside a SELECT changes nothing
    When a client calls query with "<sql>"
    Then the tool result is an error that holds "query failed"
    And the table "orders" still has 200 rows

    Examples:
      | sql                                                                    |
      | WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d      |
      | WITH u AS (UPDATE orders SET amount_cad = 0 RETURNING *) SELECT * FROM u |

  @R22
  Scenario Outline: A SELECT that fails in DuckDB comes back as an error result, and the server goes on
    When a client calls query with "<sql>"
    Then the tool result is an error that holds "query failed"
    And the last log line has tool "query" and status "failed"
    When a client calls query with "SELECT count(*) FROM orders"
    Then the result's rows are [[200]]

    Examples:
      | sql                                   |
      | SELECT no_such_column FROM orders     |
      | SELECT * FROM read_csv('/etc/passwd') |
      | SELECT * FROM no_such_table           |

  @R21
  Scenario: Queries sent at the same time each get their own answer
    When 12 clients call query at the same time, each for another store
    Then every one of them got the row of its own store
    And the table "orders" still has 200 rows

  @R23
  Scenario: The time limit of one query, as the server is started
    Then the time limit of one query is 5 seconds

  @R23
  Scenario: A query that runs longer than the time limit is stopped, and the server goes on
    Given QUERY_TIMEOUT_S is "0.3" and the main server is started from the environment
    When a client calls query with "SELECT count(*) FROM orders a, orders b, orders c, orders d, orders e"
    Then the tool result is an error that holds "query stopped after 0.3 s, the time limit of one query"
    And the last log line has tool "query" and status "timeout"
    When a client calls query with "SELECT count(*) FROM orders"
    Then the result's rows are [[200]]

  @R40
  Scenario: One log line per tool call, without the SQL
    When a client calls get_schema
    And a client calls query with "SELECT store_name FROM stores WHERE city = 'Kelowna'"
    Then the log has 2 lines
    And log line 1 has exactly the fields "tool, status, ms, tables"
    And log line 2 has exactly the fields "tool, status, ms, rows, truncated, text_cut, columns_cut"
    And log line 2 has tool "query" and status "ok"
    And no log line holds "Kelowna" or "SELECT"
