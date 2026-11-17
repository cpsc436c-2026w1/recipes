Feature: One trace item per question
  Every question leaves one item: the question, the SQL, the status, every step and a hash of the user id.
  A local run writes it to a file, a run with --target aws to the DynamoDB table $PREFIX-chat-traces.
  README, "Requirements": R50 to R55, R60, R61. Scenarios tagged @aws run against moto, a local stand-in for AWS.

  Background:
    Given the shop's tables are loaded in DuckDB
    And the stub LLM with the fixed replies of main/stub_replies.json

  @R50 @R51
  Scenario: A local run writes one trace item
    Given traces are written to a local file
    When customer "c-0007" asks "How many orders are there?"
    Then the trace file has 1 item with status "answered"
    And the trace item holds the question, the SQL and 3 steps
    And the trace item holds a 16-character "user_hash" and no "user_id"
    And the trace item expires 7 days after it was written

  @R55
  Scenario: A trace item keeps a sample of the rows
    Given traces are written to a local file
    And the LLM answers "List every order" with the query "SELECT order_id FROM orders ORDER BY order_id"
    When customer "c-0007" asks "List every order"
    Then the result has 50 rows
    And the trace item holds 5 rows of the result

  @aws @R52 @R60 @R61
  Scenario: A run on AWS creates the trace table and writes the item there
    Given PREFIX is "test" and AWS_REGION is not set
    When a run with "--target aws" is set up, with the stub LLM in place of the gateway
    Then the DynamoDB table "test-chat-traces" is ACTIVE in "ca-central-1"
    And the table "test-chat-traces" is billed per request and has time to live on "expires_at"
    When customer "c-0007" asks "How many orders are there?"
    Then the table "test-chat-traces" holds the item of that request with status "answered"

  @aws @R53
  Scenario: A run right after clean-up, while the table is still being deleted
    Given the DynamoDB table "test-chat-traces" is in the DELETING state
    When the recipe makes sure the table "test-chat-traces" exists
    Then the recipe waited until the table "test-chat-traces" was gone
    And the DynamoDB table "test-chat-traces" is ACTIVE in "ca-central-1"

  @aws @R54
  Scenario: DynamoDB refuses a trace write
    Given traces go to the DynamoDB table "test-chat-traces", which does not exist
    When customer "c-0007" asks "How many orders are there?"
    Then the status is "answered"
    And the printed output holds "trace write failed"
