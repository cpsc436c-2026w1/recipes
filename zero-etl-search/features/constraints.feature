Feature: Course constraints
  README, "Requirements": R60 and R61.

  @R60
  Scenario: The offline run needs no AWS account and no network
    Given every network connection fails the scenario
    When main/run.py runs with no options
    Then the exit code is 0

  @R61
  Scenario: Names come from PREFIX
    Given AWS is a stand-in
    And PREFIX is "team7-ana"
    When main/run.py runs with "--target aws --setup"
    Then every name the set-up created starts with "team7-ana"
