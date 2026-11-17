Feature: An Inspect eval scores the agent in code

  @R100
  Scenario: Every request is scored on its tools and its holds
    When the Inspect task runs on the stub model
    Then every request has an outcome score computed from its tools and its holds
    And the accuracy is 1

  @R101
  Scenario: On the stub model the eval makes no network call
    When the Inspect task runs on the stub model
    Then the eval opened no network connection
