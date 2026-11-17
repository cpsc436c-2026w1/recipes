Feature: Traces go to Logfire only behind a flag
  Each scenario runs one request in a process of its own, with every network connection refused and recorded.

  @R80
  Scenario: With the flag off, no span is made and nothing is sent
    Given the setting "LOGFIRE_ENABLED" is "false"
    And the setting "LOGFIRE_TOKEN" is "made-up-token-that-must-not-be-used"
    When a request runs in a process of its own
    Then no span was made and no tracer provider was set
    And no network connection was opened

  @R81
  Scenario: With the flag on, one request is one trace
    Given the setting "LOGFIRE_ENABLED" is "true"
    When a request runs in a process of its own
    Then there is one trace, under the span "member request", with 4 model spans and 3 tool spans

  @R82
  Scenario: With the flag on and no token, the spans stay on this machine
    Given the setting "LOGFIRE_ENABLED" is "true"
    And the setting "LOGFIRE_TOKEN" is empty
    When a request runs in a process of its own
    Then spans were made
    And no network connection was opened
