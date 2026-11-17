Feature: Cheap model first (ext-model-routing)
  ext-model-routing/README.md, "Requirements": R80 to R86.

  @R80 @R82
  Scenario: The router is one short call to the cheaper model; app code picks the answering model
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "simple" to "HX-200" and "hard" to anything else
    When ext-model-routing/routing_eval.py runs with "--model gateway"
    Then the exit code is 0
    And the gateway received 40 requests
    And every router call asks for "qwen3.6-35b-a3b" with max_tokens 8
    And in the routed policy the request "How much is the HX-200 charger?" is answered by "qwen3.6-35b-a3b"
    And in the routed policy every other request is answered by "qwen3.8-27b"

  @R81
  Scenario Outline: A router reply that is not "simple" sends the request to the stronger model
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "<reply>" to everything
    When ext-model-routing/routing_eval.py runs with "--model gateway"
    Then in the routed policy every request is answered by "<model>"

    Examples:
      | reply         | model           |
      | Simple.       | qwen3.6-35b-a3b |
      | simple!       | qwen3.8-27b     |
      | I am not sure | qwen3.8-27b     |
      | hard          | qwen3.8-27b     |

  @R85
  Scenario: The stronger model gets room to reason before it answers
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "hard" to everything
    When ext-model-routing/routing_eval.py runs with "--model gateway"
    Then every answer call to "qwen3.6-35b-a3b" asks for max_tokens 150
    And every answer call to "qwen3.8-27b" asks for max_tokens 600

  @R86
  Scenario: An empty reply is reported and is not scored
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose stronger model spends its tokens on reasoning for 4 of the 10 requests
    When ext-model-routing/routing_eval.py runs with "--model gateway"
    Then the exit code is 0
    And it printed "all-strong: 4 of 10 answers NOT SCORED, accuracy is over the other 6"
    And it printed "empty reply, finish_reason length"
    And the all-strong accuracy is the share of correct answers among the 6 that were scored
    And the all-cheap policy has no answer that was not scored

  @R83 @R84
  Scenario: The stub eval's table
    When ext-model-routing/routing_eval.py runs with no options and the output folder in a temporary folder
    Then the exit code is 0
    And the policy table is
      | policy     | accuracy | cost (CAD) | of which router | share sent to the stronger model |
      | all-cheap  | 0.70     | 0.000114   | 0.000000        | 0.00                             |
      | all-strong | 1.00     | 0.000234   | 0.000000        | 1.00                             |
      | routed     | 0.90     | 0.000217   | 0.000039        | 0.50                             |
    And it printed "router label agrees with the hand label on 0.80 of the requests"
    And nothing was written to the recipe's own output folder

  @R83
  Scenario: The cost of a call is its tokens at the gateway's price
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "hard" to everything
    When ext-model-routing/routing_eval.py runs with "--model gateway"
    Then the all-cheap policy cost is 10 calls of 400 input and 20 output tokens at 0.05 and 0.10 CAD per million
    And the all-strong policy cost is 10 calls of 400 input and 20 output tokens at 0.10 and 0.20 CAD per million
