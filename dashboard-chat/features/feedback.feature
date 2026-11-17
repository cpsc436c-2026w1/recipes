Feature: Extension: a user's verdict on an answer
  A thumbs up or down is stored on the trace item of the question it is about.
  README, "Requirements": R110 to R114 are in ext-feedback/README.md.
  Scenarios tagged @aws run against moto, a local stand-in for AWS.

  Background:
    Given the shop's tables are loaded in DuckDB
    And the stub LLM with the fixed replies of main/stub_replies.json

  @R110
  Scenario: A verdict on an answer of a local run, and a second one in its place
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    And the user marks that answer "down"
    Then the trace item of that answer has the verdict "down" and the time it was given
    And the trace file has 1 item with status "answered"
    When the user marks that answer "up"
    Then the trace item of that answer has the verdict "up" and the time it was given

  @aws @R110
  Scenario: A verdict on an answer in DynamoDB
    Given traces go to the DynamoDB table "test-chat-traces"
    When customer "c-0007" asks "How many orders are there?"
    And the user marks that answer "down"
    Then the trace item of that answer has the verdict "down" and the time it was given
    And the trace item of that answer still has its question, SQL and expiry time

  @R111
  Scenario: A verdict that is neither up nor down
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    And feedback_run.py is asked to mark that answer "maybe"
    Then the exit code is 1
    And the error output holds "a verdict is one of up, down"
    And the trace item of that answer has no verdict

  @aws @R112
  Scenario: A verdict for a request that has no trace item
    Given traces go to the DynamoDB table "test-chat-traces"
    When the user marks the request "no-such-request" "up"
    Then the verdict is refused because no trace item has that request id
    And the table "test-chat-traces" holds 0 items

  @R112
  Scenario: The same in a local run
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    And feedback_run.py is asked to mark the request "no-such-request" "up"
    Then the exit code is 1
    And the error output holds "no trace item of this user for request no-such-request"
    And the trace file has 1 item with status "answered"

  @aws @R112
  Scenario: A verdict for an item that has passed its expiry time and is still in the table
    Given traces go to the DynamoDB table "test-chat-traces"
    When customer "c-0007" asks "How many orders are there?"
    And the trace item of that answer passes its expiry time before time to live removes it
    And customer "c-0007" marks that answer "down"
    Then the verdict is refused because no trace item has that request id
    And the trace item of that answer has no verdict

  @aws @R114
  Scenario: Another user's verdict on an answer in DynamoDB
    Given traces go to the DynamoDB table "test-chat-traces"
    When customer "c-0007" asks "How many orders are there?"
    And customer "c-0012" marks that answer "down"
    Then the verdict is refused with the same words as for a request id that does not exist
    And the trace item of that answer has no verdict
    When customer "c-0007" marks that answer "down"
    Then the trace item of that answer has the verdict "down" and the time it was given

  @R114
  Scenario: Another user's verdict on an answer of a local run
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    And customer "c-0012" marks that answer "up"
    Then the verdict is refused because no trace item has that request id
    And the trace item of that answer has no verdict

  @R113
  Scenario: The report counts the verdicts and lists the answers marked down
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    And the user marks that answer "up"
    And customer "c-0007" asks "What were total sales in Vancouver?"
    And the user marks that answer "down"
    And customer "c-0007" asks "Which category sold the most items?"
    And feedback_run.py prints its report
    Then the first printed line is "3 answers: 1 marked up, 1 marked down, 1 not marked"
    And the printed output holds "What were total sales in Vancouver?"
    And the printed output holds "SELECT ROUND(SUM(o.amount_cad), 2)"
    And 2 lines are printed
