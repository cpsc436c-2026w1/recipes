Feature: Model calls go through the course LLM gateway
  One client, the student's own token, no second attempt on an error.
  README, "Requirements": R20 to R29, R30 to R32.

  @R20 @R26
  Scenario: A gateway run sends every call to the gateway with the course token
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "lookup"
    When main/run.py runs with "--model gateway"
    Then the exit code is 0
    And the gateway received 11 requests
    And every gateway request went to "https://gw.example/v1/chat/completions"
    And every gateway request carries the course token as the bearer key
    And every gateway request has temperature 0
    And the course token is in no printed line and in no line of the misroute log

  @R21
  Scenario: The default models
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "hard"
    When main/run.py runs with "--model gateway --request Hello"
    Then gateway request 1 asks the model "qwen3.6-35b-a3b" with max_tokens 8
    And gateway request 2 asks the model "qwen3.8-27b" with max_tokens 600

  @R21
  Scenario: SMALL_MODEL and LARGE_MODEL name other models
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And SMALL_MODEL is "qwen3.8-27b" and LARGE_MODEL is "qwen3.6-35b-a3b"
    And the gateway is a stand-in whose router replies "hard"
    When main/run.py runs with "--model gateway --request Hello"
    Then gateway request 1 asks the model "qwen3.8-27b" with max_tokens 8
    And gateway request 2 asks the model "qwen3.6-35b-a3b" with max_tokens 600

  @R22
  Scenario Outline: An error status from the gateway ends the request, with no second attempt
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers every call with HTTP <status>
    When main/run.py runs with "--model gateway"
    Then 6 printed lines are JSON with path "error" and an error that names "<status>"
    And the gateway received 6 requests
    And the exit code is 1

    Examples:
      | status |
      | 429    |
      | 500    |
      | 502    |

  @R22
  Scenario Outline: A reply with status 200 that is not a chat completion ends the request the same way
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers every call with status 200 and <reply>
    When main/run.py runs with "--model gateway --request Hello"
    Then 1 printed line is JSON with path "error" and an error that names "not a chat completion"
    And the gateway received 1 request
    And the exit code is 1

    Examples:
      | reply                    |
      | a page that is not JSON  |
      | an error object          |
      | an empty list of choices |

  @R22 @R18 @R32
  Scenario: A hard call that fails keeps the router's call and cost, and the route log says error
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers HTTP 429 to the large model only
    When main/run.py runs with "--model gateway --request Hello"
    Then 1 printed line is JSON with path "error" and an error that names "429"
    And the printed line of the request names 1 model call, the router's, and its cost
    And the route log's line for the request has path "error" and error true
    And the gateway received 2 requests

  @R17 @R22
  Scenario: A misroute whose hard call fails is logged as one that ended in error
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose router replies "lookup" and whose large model answers HTTP 500
    When main/run.py runs with "--model gateway --request Hello"
    Then 1 printed line is JSON with path "error" and an error that names "500"
    And the misroute log has 1 line that says the request ended in error

  @R29
  Scenario Outline: A refusal of the key or the model stops the run at that request
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers every call with HTTP <status>
    When main/run.py runs with "--model gateway"
    Then 1 printed line is JSON with path "error" and an error that names "<status>"
    And the gateway received 1 request
    And the printed output holds "stopped after 1 of 6 requests"
    And the error output holds "1 of 1 requests ended in error"
    And the exit code is 1

    Examples:
      | status |
      | 401    |
      | 403    |

  @R28
  Scenario: One request that ends in error does not stop the others
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that answers HTTP 429 to requests about "weather"
    When main/run.py runs with "--model gateway"
    Then 1 printed line is JSON with path "error" and an error that names "429"
    And 5 printed lines are JSON with path "hard" and no error
    And the last printed line starts with "model: gateway; 6 requests; large-model calls 5"
    And the error output holds "1 of 6 requests ended in error"
    And the exit code is 1

  @R23
  Scenario Outline: A gateway run without its two variables
    Given <set> is set to a made-up value and <missing> is not set
    And every network connection fails the scenario
    When main/run.py runs with "--model gateway"
    Then the exit code is 1
    And the error output holds "COURSE_GATEWAY_URL"
    And the error output holds "KB_MCP_TOKEN"

    Examples:
      | set              | missing          |
      | GATEWAY_URL      | COURSE_MCP_TOKEN |
      | COURSE_MCP_TOKEN | GATEWAY_URL      |

  @R24
  Scenario: A gateway that does not answer
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in that does not answer within the time limit of one call
    When main/run.py runs with "--model gateway --request Hello"
    Then 1 printed line is JSON with path "error" and an error that names "ReadTimeout"
    And the gateway received 1 request
    And the time limit the recipe set for one call is 250 seconds
    And the exit code is 1

  @R25
  Scenario: A call is priced as the model it asked for
    Given a gateway stand-in that names its models differently in its replies
    When the app handles "Compare the rain jacket and the insulated jacket."
    Then the router call costs 0.00006 CAD for 1000 input and 100 output tokens
    And the hard call costs 0.00012 CAD for 1000 input and 100 output tokens
    And the request made 1 large-model call

  @R27
  Scenario Outline: A model without a price is refused before any call
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the variable <variable> names the model "tiny-1"
    And the gateway is a stand-in whose router replies "hard"
    When main/run.py runs with "--model gateway"
    Then the exit code is 1
    And the error output holds "no price for model 'tiny-1'"
    And the gateway received 0 requests

    Examples:
      | variable    |
      | SMALL_MODEL |
      | LARGE_MODEL |

  @R27
  Scenario: The stub run checks the prices too
    Given the variable LARGE_MODEL names the model "big-1"
    When main/run.py runs on the stub
    Then the exit code is 1
    And the error output holds "no price for model 'big-1'"

  @R30
  Scenario: The stub run needs no account, no token and no network
    Given no course token and no gateway address in the environment
    And every network connection fails the scenario
    When main/run.py runs on the stub
    Then the exit code is 0
    And the last printed line starts with "model: stub; 6 requests"

  @R32
  Scenario: Every request leaves one line in the route log, without its text
    When main/run.py runs on the stub
    Then the route log has 6 lines, one per request, each with "ts, request_id, label, path, misrouted, error, large_model_calls, cost_cad, ms"
    And the route log counts 3 requests on the path "hard", 2 of them misrouted
    And no request's text is in the route log
    And the route log's costs add up to the total the run printed

  @R31
  Scenario: A second run adds to the misroute log
    When main/run.py runs on the stub
    And main/run.py runs on the stub
    Then the misroute log file has 4 lines
