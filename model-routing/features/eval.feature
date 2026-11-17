Feature: The routing eval
  Label accuracy on hand-labelled requests, and the cost of routing against sending everything to the large model.
  ext-eval/README.md, "Requirements": R50 to R57.

  @R50
  Scenario: Label accuracy, overall and per hand label
    When the routing eval runs on the stub
    Then the exit code is 0
    And the report holds "Routing eval, 24 labelled requests"
    And the report holds "Label accuracy: 0.88"
    And the report holds "| search | 6 | 5 | hard 1, search 5 |"
    And the report holds "| lookup | 6 | 6 | lookup 6 |"

  @R51 @R55
  Scenario: Each request runs under both policies
    When the routing eval runs on the stub
    Then the report's row "all-large (before routing)" has 24 large-model calls and router cost 0
    And the report's row "routed" has 8 large-model calls and router cost 0.000151
    And the report holds "Routed cost is 0.50 of all-large."
    And the large model was called 32 times in the eval

  @R52
  Scenario: Accuracy below the gate
    When the routing eval runs on the stub with "--min-accuracy 0.9"
    Then the exit code is 1
    And the printed output holds "FAIL: accuracy 0.88 below 0.90"

  @R52
  Scenario Outline: Accuracy at or above the gate
    When the routing eval runs on the stub with "--min-accuracy <gate>"
    Then the exit code is <code>

    Examples: the stub labels 21 of 24 right, which is 0.875
      | gate   | code |
      | 0.85   | 0    |
      | 0.875  | 0    |
      | 0.876  | 1    |

  @R53
  Scenario: A request labelled differently from its hand label
    When the routing eval runs on the stub
    Then the report holds "- r17 hand hard, router lookup, ran lookup:"
    And the eval's log has 3 lines with reason "label differs from the hand label"
    And the eval's log names the requests "r11, r17, r22" for that reason

  @R54
  Scenario: Large-model calls on requests that are out of scope by hand
    When the routing eval runs on the stub
    Then the report holds "Large-model calls on requests hand-labelled out_of_scope: 1"

  @R57
  Scenario: The routed cost by the path that ran
    When the routing eval runs on the stub
    Then the report holds "| hard | 8 | 0.000430 | 0.78 | 8 |"
    And the report holds "| lookup | 7 | 0.000043 | 0.08 | 0 |"
    And the report's paths have 24 requests in all, and their shares add up to 1.00

  @R56
  Scenario: A model call that fails stops the eval
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers every call with HTTP 429
    When the routing eval runs with "--model gateway"
    Then the exit code is 1
    And the error output holds "eval stopped at r01"
    And the error output holds "429"
    And the gateway received 1 request
    And no report was written

  @R56
  Scenario: A model call that fails in the all-large runs stops the eval too
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "out_of_scope" and whose large model answers HTTP 429
    When the routing eval runs with "--model gateway"
    Then the exit code is 1
    And the error output holds "eval stopped at r01 (all-large)"
    And the gateway received 25 requests
    And no report was written
