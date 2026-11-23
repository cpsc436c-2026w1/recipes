Feature: Model calls go through the course LLM gateway
  The student's own token, read from the environment; the offline stub needs neither.
  README, "Requirements": R30 to R36, R40.

  @R30 @R32 @R33
  Scenario: A gateway run sends every call to the gateway with the course token
    Given a gateway stand-in that answers as the stub does
    When main/run.py runs with "--target gateway"
    Then the exit code is 0
    And the gateway received 149 requests
    And every gateway request went to "/v1/chat/completions" with the course token as the bearer key
    And the gateway requests named the models "qwen3.6-35b-a3b, qwen3.8-27b"
    And 8 gateway requests named the model "qwen3.8-27b"
    And 141 gateway requests named the model "qwen3.6-35b-a3b"
    And every gateway request caps the output at 16384 tokens
    And the report's heading says "6 questions, 3 runs per prompt, task model qwen3.6-35b-a3b, course LLM gateway"
    And the report's row "after" is "83% | 83% / 83% / 83% | 15/18 | wrong value 3"
    And the course token is in no printed line and in no file of the run folder

  @R32
  Scenario: --task-model and --reflection-model name other models
    Given a gateway stand-in that answers as the stub does
    When main/run.py runs with "--target gateway --runs 1 --task-model qwen3.8-27b --reflection-model qwen3.6-35b-a3b --max-metric-calls 20"
    Then the gateway requests named the models "qwen3.6-35b-a3b, qwen3.8-27b"
    And the spend file prices "task" as "qwen3.8-27b"

  @R31
  Scenario Outline: A gateway run without its two variables
    Given a gateway stand-in that answers as the stub does
    And <variable> is not set
    When main/run.py runs with "--target gateway"
    Then the exit code is 1
    And the error output holds "COURSE_GATEWAY_URL"
    And the error output holds "KB_MCP_TOKEN"
    And the gateway received 0 requests
    And no run folder was written

    Examples:
      | variable           |
      | COURSE_GATEWAY_URL |
      | KB_MCP_TOKEN       |

  @R31
  Scenario: A gateway run with the older variable names
    Given a gateway stand-in that answers as the stub does
    And its address and the token are set as GATEWAY_URL and COURSE_MCP_TOKEN only
    When main/run.py runs with "--target gateway --runs 1 --max-metric-calls 20"
    Then the exit code is 0
    And every gateway request went to "/v1/chat/completions" with the course token as the bearer key

  @R34
  Scenario: A gateway that starts refusing calls cuts the search and stops the test runs
    Given a gateway stand-in that refuses every request after the first 40 with HTTP 429
    When main/run.py runs with "--target gateway --runs 1"
    Then the exit code is 1
    And the report holds "(every role counted) failed, so the search was cut there (task: LMRateLimitError"
    And the report holds "**The test runs stopped before they were done:**"
    And the gateway received fewer than 90 requests
    And instructions_after.txt is not empty

  @R36
  Scenario: An empty reflection reply cuts the search and is named in the report
    Given a gateway stand-in whose reflection replies are empty, cut at the token cap
    When main/run.py runs with "--target gateway --runs 1"
    Then the exit code is 0
    And the gateway received fewer than 40 requests
    And the report holds "failed, so the search was cut there (reflect: EmptyReflection: empty reply"
    And the report holds "REFLECTION_MAX_TOKENS"

  @R35
  Scenario Outline: A call the gateway fails is sent once, not again
    # One call is held once against the money cap; the gateway charges each sent attempt that fails this way.
    Given a gateway stand-in that refuses every request after the first 40 with HTTP <status>
    When main/run.py runs with "--target gateway --runs 1"
    Then the exit code is 1
    And the gateway received more than 40 requests, and none of them twice after the first 40
    And the spend file holds nothing for calls in flight

    Examples:
      | status |
      | 500    |
      | 429    |

  @R40
  Scenario: The stub run needs no account, no token and no network
    Given no course token and no gateway address in the environment
    And every network connection fails the scenario
    When main/run.py runs with "--runs 1"
    Then the exit code is 0
    And the report's row "after" is "83% | 83% | 5/6 | wrong value 1"
