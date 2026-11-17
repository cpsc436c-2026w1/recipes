Feature: Clean-up removes what the recipe created, and nothing else
  clean_up.py as the command line runs it, against moto. README, "Requirements": R50 to R56.

  Background:
    Given a run with "--target aws" has set up everything for PREFIX "test"

  @R50
  Scenario: Clean-up after a real run
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "11 resources removed for PREFIX=test in ca-central-1"
    And the account has no alarm, dashboard, function, log group, budget, topic or role

  @R51
  Scenario: A dry run lists what it would remove and removes nothing
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And the printed output holds "would remove lambda function test-app"
    And the printed output holds "11 resources to remove for PREFIX=test"
    And no Delete call was made

  @R52
  Scenario: A function with the recipe's name and without its tag stays, with its log group
    Given the function "test-app" has lost the tag "recipe"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED lambda function test-app: refused"
    And the error output holds "FAILED log group /aws/lambda/test-app: refused"
    And the function "test-app" and its log group are still there
    And the printed output holds "removed budget test-monthly"

  @R53
  Scenario: AWS refuses a step
    Given AWS refuses DeleteBudget with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED budget test-monthly: AccessDeniedException"
    And the printed output holds "removed sns topic test-limits-alarms"
    And the printed output holds "removed iam role test-app-role"

  @R53
  Scenario: The function's tag cannot be read, so its log group stays
    Given AWS refuses ListTags with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED lambda function test-app: AccessDeniedException"
    And the error output holds "FAILED log group /aws/lambda/test-app: refused: the tag of the function test-app could not be read"
    And the function "test-app" and its log group are still there

  @R54
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs again
    Then the exit code is 0
    And the printed output of the second clean-up holds "absent lambda function test-app"
    And the printed output of the second clean-up holds "0 resources removed"

  @R55
  Scenario: A role with the recipe's name and without its tag stays
    Given the role "test-app-role" has lost the tag "recipe"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED iam role test-app-role: refused"
    And the role "test-app-role" is still there

  @R55
  Scenario: A topic with the recipe's name and without its tag stays
    Given the topic "test-limits-alarms" has lost the tag "recipe"
    When clean_up.py runs
    Then the exit code is 1
    And the error output holds "FAILED sns topic test-limits-alarms: refused"
    And the topic "test-limits-alarms" is still there

  @R56
  Scenario: A team's own alarm with the same beginning stays
    Given the team has its own alarm "test-app-errors-p99"
    When clean_up.py runs
    Then the alarms of the account are "test-app-errors-p99"
