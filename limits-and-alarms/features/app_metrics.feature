Feature: One metric line per request, from the app's own code
  The function $PREFIX-app (main/app.py, main/emf.py) after a run with --target aws, invoked through the Lambda API.
  README, "Requirements": R10 to R15. Against moto; each Invoke runs the handler in-process with the function's
  environment variables, as Lambda would (features/support.py says where it differs).

  Background:
    Given a run with "--target aws" has set up everything for PREFIX "test"

  @R10 @R13
  Scenario: A request that succeeds writes one line with its metrics
    When the function is invoked with {"tokens_in": 800, "tokens_out": 120}
    Then the function answers 200
    And the invocation wrote 1 log line
    And the line declares the metrics "Requests, Errors, LatencyMs, CostCAD, TokensIn, TokensOut" in the namespace "test/app" with the dimensions "Service"
    And the line has Service "test-app", Requests 1, Errors 0, TokensIn 800 and TokensOut 120
    And the line's requestId is the invocation's request id, as a plain field

  @R11
  Scenario: A request whose work fails is counted as an error
    When the function is invoked with {"fail": true}
    Then the function answers 500
    And the invocation wrote 1 log line
    And the line has Errors 1 and errorType "RuntimeError"

  @R12
  Scenario: A request for a model without a price is refused before the work
    When the function is invoked with {"model": "some-unpriced-model", "tokens_in": 800, "tokens_out": 120}
    Then the function answers 500
    And the line has Errors 1 and errorType "KeyError"
    And the line has CostCAD 0, TokensIn 0 and TokensOut 0

  @R14
  Scenario Outline: The cost of a request comes from the price table
    When the function is invoked with {"model": "<model>", "tokens_in": 800, "tokens_out": 120}
    Then the line has CostCAD <cost>

    Examples:
      | model           | cost     |
      | qwen3.6-35b-a3b | 0.000052 |
      | qwen3.8-27b     | 0.000104 |

  @R15
  Scenario: The metric line is one JSON object on a line of its own
    When the function is invoked with {}
    Then every log line of the invocation is one JSON object with nothing before or after it
