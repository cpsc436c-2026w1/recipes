Feature: Extension: answer cache
  The cache is looked up before the model. Its key is the normalized question, the data version, the model and
  prompt version and who asks.
  README, "Requirements": R80 to R86; ext-answer-cache/README.md.

  Background:
    Given the shop's tables are loaded in DuckDB
    And the stub LLM with the fixed replies of main/stub_replies.json
    And an empty answer cache

  @R80
  Scenario: The same question again, with other capitals and spacing, from another user
    When customer "c-0007" asks through the cache "How many orders are there?"
    And customer "c-0012" asks through the cache "how many  orders are there"
    Then result 1 has status "answered"
    And result 2 has status "cache_hit"
    And result 2 has the rows of result 1
    And result 2 has cost 0
    And the LLM was called 2 times

  @aws @R80
  Scenario: The cache in DynamoDB
    Given the answer cache is the DynamoDB table "test-chat-cache"
    When customer "c-0007" asks through the cache "How many orders are there?"
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 2 has status "cache_hit"
    And result 2 has the rows of result 1

  @R81
  Scenario: A question with "I" is kept for the user who asked
    When customer "c-0007" asks through the cache "How much did I spend in total?"
    And customer "c-0012" asks through the cache "How much did I spend in total?"
    And customer "c-0007" asks through the cache "How much did I spend in total?"
    Then result 2 has status "answered"
    And result 2 has other rows than result 1
    And result 3 has status "cache_hit"
    And result 3 has the rows of result 1

  @R82
  Scenario: A question with a clock word is sent to the model every time
    Given the LLM answers "How many orders are there today?" with the query "SELECT COUNT(*) FROM orders"
    When customer "c-0007" asks through the cache "How many orders are there today?"
    And customer "c-0007" asks through the cache "How many orders are there today?"
    Then result 2 has status "answered"
    And the cache holds 0 items

  @R83
  Scenario: A result that is not an answer is not stored
    When customer "c-0007" asks through the cache "Delete all the orders"
    And customer "c-0007" asks through the cache "Delete all the orders"
    Then result 2 has status "no_answer"
    And the cache holds 0 items

  @R83
  Scenario: A stored answer is used for 24 hours
    When customer "c-0007" asks through the cache "How many orders are there?"
    Then the cache holds 1 item
    When 23 hours pass
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 2 has status "cache_hit"
    When 2 hours pass
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 3 has status "answered"

  @R84
  Scenario: New data, new key
    When customer "c-0007" asks through the cache "How many orders are there?"
    And the Parquet files change
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 2 has status "answered"

  @R84
  Scenario: A new model, new key
    When customer "c-0007" asks through the cache "How many orders are there?"
    And the model changes to "qwen3.8-27b"
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 2 has status "answered"

  @R84
  Scenario: A new rule in the system prompt, new key
    When customer "c-0007" asks through the cache "How many orders are there?"
    And the rule "Reply in French." is added to the system prompt
    And customer "c-0007" asks through the cache "How many orders are there?"
    Then result 2 has status "answered"

  @R80 @R84
  Scenario: cache_run.py gives every look-up the model's name and the prompt version
    When cache_run.py runs with the stub LLM
    Then the exit code is 0
    And the last printed line is "7 cache hits, 11 questions sent to the model"
    And every look-up of the run carried the stub model's name and the version of the system prompt

  @R86
  Scenario: In front of the guardrails, a question without "I" is still kept for the user who asked
    When customer "c-0007" asks through the cache in front of the guardrails "How many orders are there?"
    And customer "c-0012" asks through the cache in front of the guardrails "How many orders are there?"
    And customer "c-0007" asks through the cache in front of the guardrails "How many orders are there?"
    Then result 2 has status "answered"
    And result 2 has other rows than result 1
    And result 3 has status "cache_hit"
    And result 3 has the rows of result 1

  @aws @R85
  Scenario: The cache table cannot be read or written
    Given the answer cache is the DynamoDB table "test-chat-cache", which does not exist
    When customer "c-0007" asks through the cache "How many orders are there?"
    Then result 1 has status "answered"
    And the printed output holds "cache get failed"
    And the printed output holds "cache put failed"
