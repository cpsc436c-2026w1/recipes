Feature: Compare two runs
  The same endpoint a week later, or two endpoints: which rows changed.
  README, "Requirements": R39.

  Background:
    Given LLM_BASE_URL and LLM_API_KEY are set to the stand-in endpoint and its key

  @R39
  Scenario: Two runs of the same endpoint do not differ
    Given a run of every group saved as "monday.json"
    And a run of every group saved as "today.json"
    When main/compare.py compares "monday.json" and "today.json"
    Then the exit code is 0
    And the printed output holds "no row differs between"

  @R39
  Scenario: An endpoint that lost features since the first run
    Given a run of every group saved as "monday.json"
    And the endpoint does nothing right beyond listing models and chatting
    And a run of every group saved as "today.json"
    When main/compare.py compares "monday.json" and "today.json"
    Then the exit code is 1
    And the comparison lists "usage in the stream, max_tokens stops the reply, tool call, tool result used, output fits a JSON schema, schema wins over the prompt, prompt cache reported, embeddings"
    And the comparison says "tool call" went from "yes" to "no"
    And the comparison says "tool result used" went from "yes" to "not run"
    And the last printed line is "8 row(s) differ"
    And the comparison sent no request to the endpoint

  @R39
  Scenario: One of two chat models changed between the runs
    Given the endpoint lists the chat models "fake-chat" and "fake-chat-b"
    And a run of the groups "main,tool-calls" saved as "monday.json"
    And the endpoint answers "fake-chat-b" with plain text where a tool is offered
    And a run of the groups "main,tool-calls" saved as "today.json"
    When main/compare.py compares "monday.json" and "today.json"
    Then the exit code is 1
    And the comparison lists only rows of "fake-chat-b"
    And for "fake-chat-b" the comparison row "tool call" reads "yes" then "no"
    And the last printed line is "2 row(s) differ"

  @R39
  Scenario: A run that left a group out
    Given a run of every group saved as "monday.json"
    And a run of the groups "main" saved as "today.json"
    When main/compare.py compares "monday.json" and "today.json"
    Then the exit code is 1
    And the comparison says "streaming" went from "yes" to "not run"
