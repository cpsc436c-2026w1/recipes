Feature: Clean-up removes the recipe's tables, and nothing else
  clean_up.py as the command line runs it, against moto. README, "Requirements": R40 to R45.

  Background:
    Given the main scenario and both extensions have run for PREFIX "test"

  @R40
  Scenario: Clean-up after a real run
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "4 tables removed for PREFIX=test in ca-central-1"
    And the account has no table

  @R41
  Scenario: A dry run lists the tables and deletes none
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And the printed output holds "would remove dynamodb table test-chat-conversations"
    And the printed output holds "4 tables to remove for PREFIX=test"
    And DeleteTable was not called
    And the account has the tables "test-answer-cache, test-chat-conversations, test-chat-history, test-llm-traces"

  @R42
  Scenario: A table that an earlier clean-up is still deleting
    Given the table "test-chat-conversations" is in the DELETING state
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "absent dynamodb table test-chat-conversations (already being deleted)"
    And DeleteTable was not called for "test-chat-conversations"
    And the printed output holds "3 tables removed"

  @R43
  Scenario: AWS refuses a step
    Given AWS refuses DeleteTable for "test-chat-history" with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED dynamodb table test-chat-history: AccessDeniedException"
    And the account has the tables "test-chat-history"

  @R44
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs again
    Then the exit code is 0
    And the printed output of the second clean-up holds "absent dynamodb table test-llm-traces"
    And the printed output of the second clean-up holds "0 tables removed"

  @R45
  Scenario: A team's own table with the same beginning stays
    Given the team has its own table "test-chat-history-archive"
    When clean_up.py runs
    Then the account has the tables "test-chat-history-archive"
