Feature: Chat on the dashboard, the main scenario
  A user asks a question in plain words. The LLM writes one read-only SQL query through one tool, app code
  checks the query, DuckDB runs it, and the reply quotes the rows.
  README, "Requirements": R10 to R16, R20 to R23, R30 to R36, R40 to R44, R62.

  Background:
    Given the shop's tables are loaded in DuckDB
    And the stub LLM with the fixed replies of main/stub_replies.json

  @R10 @R11
  Scenario: A question the tables can answer
    When customer "c-0007" asks "What were total sales in Vancouver?"
    Then the status is "answered"
    And the rows hold the value of the check query "SELECT ROUND(SUM(o.amount_cad), 2) FROM orders o JOIN stores s USING (store_id) WHERE s.city = 'Vancouver'"
    And the reply holds that value
    And the SQL that ran starts with "SELECT"
    And the steps are "model_call, tool_call, model_call"
    And the tool call step in the trace says nothing was cut

  @R10
  Scenario Outline: "I" in a question means the customer who asks
    When customer "<customer>" asks "How much did I spend in total?"
    Then the status is "answered"
    And the rows hold the value of the check query "SELECT ROUND(SUM(amount_cad), 2) FROM orders WHERE customer_id = '<customer>'"

    Examples:
      | customer |
      | c-0007   |
      | c-0012   |

  @R12
  Scenario: A question the tables cannot answer
    When customer "c-0007" asks "What is the weather today?"
    Then the status is "no_answer"
    And the reply is "I can only answer questions about the shop's sales."
    And the rows are empty
    And the steps are "model_call"

  @R13 @R16
  Scenario: A query that returns more rows than the limit
    Given the LLM answers "List every order" with the query "SELECT order_id FROM orders ORDER BY order_id"
    When customer "c-0007" asks "List every order"
    Then the result has 50 rows
    And the tool result the LLM received has 50 rows and says "rows" were cut
    And the tool call step in the trace says "rows" were cut

  @R15 @R16
  Scenario: A text value longer than the limit
    Given the LLM answers "Show the longest note" with the query "SELECT repeat('x', 500) AS note FROM orders LIMIT 1"
    When customer "c-0007" asks "Show the longest note"
    Then the first value in the rows has 200 characters
    And the tool result the LLM received says "text" was cut
    And the tool call step in the trace says "text" was cut

  @R15 @R16
  Scenario: More rows than the limit, with text values longer than the limit
    Given the LLM answers "Show every note" with the query "SELECT repeat('x', 201) AS note FROM orders"
    When customer "c-0007" asks "Show every note"
    Then the result has 50 rows
    And the first value in the rows has 200 characters
    And the tool call step in the trace says "rows and text" were cut

  @R20
  Scenario Outline: SQL that is not one SELECT over the team's tables is rejected and not run
    Given the LLM answers "Do it" with the query "<sql>"
    When customer "c-0007" asks "Do it"
    Then the tool call has status "rejected"
    And the tool result the LLM received starts with "SQL check:"
    And no SQL ran
    And the table "orders" still has 1200 rows

    Examples:
      | sql                                   |
      | DELETE FROM orders                    |
      | SELECT 1; DROP TABLE orders           |
      | SELECT * FROM read_csv('/etc/passwd') |
      | SELECT getenv('HOME')                 |
      | SELECT * FROM customers               |
      | COPY orders TO 'out.csv'              |

  @R21
  Scenario Outline: The locked engine refuses what the SQL check exists to stop
    When the engine is asked, without the SQL check, to run "<sql>"
    Then DuckDB raises an error that holds "<words>"
    And the table "orders" still has 1200 rows

    Examples:
      | sql                                     | words         |
      | SELECT * FROM read_csv('/etc/hostname') | disabled      |
      | COPY orders TO 'orders.csv'             | disabled      |
      | SET enable_external_access = true       | locked        |
      | SET lock_configuration = false          | locked        |
      | DELETE FROM orders                      | read-only     |
      | DROP TABLE orders                       | read-only     |
      | CREATE TABLE copy AS SELECT 1           | read-only     |
      | ROLLBACK; DELETE FROM orders            | one statement |

  @R22
  Scenario: A DuckDB error goes back to the LLM
    Given the LLM answers "Sales by region" with the query "SELECT region FROM orders"
    When customer "c-0007" asks "Sales by region"
    Then the tool call has status "error"
    And the tool result the LLM received starts with "DuckDB:"
    And no SQL ran

  @R23
  Scenario: The LLM calls a tool the recipe does not have
    Given an LLM that calls the tool "send_email" and then replies "Done."
    When customer "c-0007" asks "Email the totals to my manager"
    Then the tool call has status "rejected"
    And the tool result the LLM received starts with "unknown tool send_email"
    And no SQL ran

  @R30
  Scenario: The LLM asks for the tool in every model call
    Given an LLM that asks for the tool on every call
    When customer "c-0007" asks "How many orders are there?"
    Then the LLM was called 4 times
    And 3 queries ran
    And the status is "no_answer"
    And the rows are empty
    And the reply is "I could not answer within the step limit."

  @R31 @R32 @R40
  Scenario: What a request to the gateway holds
    Given a gateway stand-in that answers every call with HTTP 200
    When customer "c-0007" asks "How many orders are there?"
    Then the gateway stand-in received 1 request
    And the request went to "https://gw.example/v1/chat/completions"
    And the request's Authorization header is "Bearer tok-123"
    And the request body field "max_tokens" is 16384
    And the request body field "temperature" is 0
    And the request body field "model" is "qwen3.6-35b-a3b"

  @R32
  Scenario: CHAT_MODEL names another model
    Given CHAT_MODEL is "qwen3.8-27b"
    And a gateway stand-in that answers every call with HTTP 200
    When customer "c-0007" asks "How many orders are there?"
    Then the request body field "model" is "qwen3.8-27b"

  @R35
  Scenario: A model that reasons before it answers gets room for the reasoning
    Given CHAT_MODEL is "qwen3.8-27b"
    And a gateway stand-in that asks for a query, then replies with no text and finish_reason "length"
    When customer "c-0007" asks "How many orders are there?"
    Then the gateway stand-in received 2 requests
    And every request body has the field "max_tokens" 16384

  @R36
  Scenario: A reply that used its token cap before any text
    Given a gateway stand-in whose reply has no text and finish_reason "length"
    And traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    Then the status is "error"
    And the error holds "the model used its token cap before answering"
    And the reply is empty
    And the last model call step has status "error"
    And the trace file has 1 item with status "error"

  @R36
  Scenario: A tool call cut at the token cap is not run and not tried again
    Given a gateway stand-in whose tool call is cut in its arguments, with finish_reason "length"
    When customer "c-0007" asks "How many orders are there?"
    Then the gateway stand-in received 1 request
    And 0 queries ran
    And the status is "error"
    And the error holds "the model used its token cap before answering"
    And the rows are empty

  @R36
  Scenario: A query ran, and the reply that would read it used its token cap
    Given a gateway stand-in that asks for a query, then replies with no text and finish_reason "length"
    When customer "c-0007" asks "How many orders are there?"
    Then 1 queries ran
    And the status is "error"
    And the error holds "the model used its token cap before answering"
    And the reply is empty
    And the rows are empty

  @R33
  Scenario: The gateway does not answer a model call in time
    Given a gateway stand-in that does not answer within the time limit of one call
    And traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    Then the time limit of one model call is 250 seconds
    And the status is "error"
    And the error holds "ReadTimeout"
    And the trace file has 1 item with status "error"

  @R34
  Scenario: The time limit of one query
    Then an engine that is given no time limit stops a query after 5 seconds

  @R34
  Scenario: A query that runs longer than the time limit of one query
    Given the engine stops a query after 0.2 seconds
    And the LLM answers "Count every pair of pairs of orders" with the query "SELECT COUNT(*) FROM orders a, orders b, orders c, orders d"
    When customer "c-0007" asks "Count every pair of pairs of orders"
    Then the tool call has status "error"
    And the tool result the LLM received starts with "DuckDB: query stopped after 0.2 s"
    And no SQL ran
    And the question took less than 5 seconds
    And the table "orders" still has 1200 rows

  @R41 @R50
  Scenario Outline: The gateway refuses or fails a model call
    Given a gateway stand-in that answers every call with HTTP <status>
    And traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    Then the status is "error"
    And the error holds "<status>"
    And the gateway stand-in received 1 request
    And the trace file has 1 item with status "error"

    Examples:
      | status |
      | 401    |
      | 403    |
      | 429    |
      | 500    |

  @R42
  Scenario: A run in which one question fails
    Given an LLM that fails the question "How many orders are there?" with "HTTP 429 rate_limit_exceeded"
    When main/run.py asks "How many orders are there?" and "What were total sales in Vancouver?"
    Then printed line 1 has status "error" and an "error" field that holds "HTTP 429"
    And printed line 2 has status "answered" and no "error" field
    And the exit code is 1

  @R43
  Scenario: A gateway run without its two settings
    When the gateway client is made without GATEWAY_URL and COURSE_MCP_TOKEN
    Then the exit code is 1
    And the error output holds "COURSE_GATEWAY_URL"
    And the error output holds "KB_MCP_TOKEN"

  @R44
  Scenario: The course token is in no result, trace item or printed line
    Given a gateway stand-in that answers every call with HTTP 429
    And traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    Then "tok-123" is not in the result, the trace file or the printed output

  @R14 @R62
  Scenario: The stub run needs no account, no token and no network, and prints its total cost
    Given no course token and no gateway address in the environment
    And every network connection fails the scenario
    When main/run.py runs with "--target local --model stub"
    Then the exit code is 0
    And 6 questions are printed, each with a status
    And the last printed line starts with "model: stub; total"
    And the last printed line gives, in CAD, the sum of the cost of the run's 6 trace items
    And the last printed line names the file the traces are in
