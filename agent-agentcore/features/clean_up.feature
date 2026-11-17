Feature: clean_up.py removes everything the deploy created

  @R90
  Scenario: After clean_up.py nothing of the deploy is left
    Given the recipe is deployed
    When clean_up.py runs
    Then nothing the deploy created is left
    And the output has one "removed" line for each resource
    And the exit code is 0

  @R91
  Scenario: A refused delete does not stop the others, and the exit code is 1
    Given the recipe is deployed
    And the sandbox refuses "DeleteAgentRuntime"
    When clean_up.py runs
    Then the Runtime is still there and the other resources are gone, with one "FAILED" line
    And the exit code is 1

  @R92
  Scenario: A role the recipe did not create is used and left alone
    Given a role "my-own-lambda-role" that the recipe did not create is named in "LAMBDA_ROLE_ARN"
    When the deploy runs
    And clean_up.py runs
    Then the role "my-own-lambda-role" is the only role left
    And the exit code is 0

  @R93
  Scenario: A dry run lists and deletes nothing
    Given the recipe is deployed
    When clean_up.py runs with --dry-run
    Then everything the deploy created is still there
    And the output has one "would remove" line for each resource
