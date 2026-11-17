Feature: Call the course LLM gateway with your course token, the main scenario
  The client lists the key's models, sends one chat with max_tokens set, and says what each refusal means.
  README, "Requirements": R10 to R15, R20 to R23, R30 to R33, R40 and R41. The gateway here is the gateway's own
  code (ext-deploy) on 127.0.0.1, with a fake upstream; its models are example-small, example-large, example-embed.

  @R10
  Scenario: The models, then one chat
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 0
    And the printed lines are, in order, "1. models this key may call", the three models, "2. one chat on example-small, max_tokens 300", a reply and its tokens
    And the printed tokens are "42 in, 3 out" with a request id

  @R13
  Scenario: The client prints what the gateway says it charged
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    When main/call_gateway.py runs with "--model example-small"
    Then the last printed line is "charged to your key: 0.000004 in your key's currency (the gateway's own count for this call)"
    And the key's spend is 4 micro-units

  @R13
  Scenario: A gateway that does not name the charge
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And the gateway's replies carry no "x-gateway-cost-micro" header
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 0
    And the last printed line starts with "tokens: 42 in, 3 out; request id"

  @R14
  Scenario: A reply that used its token cap before any text
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And the model service answers every chat with no text and finish_reason "length"
    When main/call_gateway.py runs with "--model example-large"
    Then the exit code is 1
    And the printed output holds "error: the model used its token cap before answering (finish_reason length)"
    And the printed output holds "tokens: 18 in, 300 out"
    And the printed output holds "what to do: run it again with a larger --max-tokens"
    And the printed output does not hold "reply:"

  @R11
  Scenario: max_tokens is always sent
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    When main/call_gateway.py runs with "--model example-small"
    Then the upstream received a chat with max_tokens 300 and temperature 0

  @R11
  Scenario: --max-tokens names another limit
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    When main/call_gateway.py runs with "--model example-small --max-tokens 40"
    Then the upstream received a chat with max_tokens 40 and temperature 0

  @R12
  Scenario: The default model is the cheaper course model
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    When main/call_gateway.py runs
    Then the printed output holds "2. one chat on qwen3.6-35b-a3b, max_tokens 300"
    And the printed output holds "refused: HTTP 403 model_not_allowed"
    And the printed output holds "model 'qwen3.6-35b-a3b' is not allowed for this key"

  @R20 @R22
  Scenario: The token travels only in the Authorization header and is never printed
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-small"
    Then the client sent 2 requests, both to the gateway's address
    And each carried the token as "Authorization: Bearer" and nowhere else
    And the token is in no printed line

  @R21
  Scenario Outline: A run without its two variables
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And <variable> is not set
    And every request the client sends is recorded
    When main/call_gateway.py runs
    Then the exit code is 1
    And the error output holds "COURSE_GATEWAY_URL"
    And the error output holds "KB_MCP_TOKEN"
    And the client sent 0 requests

    Examples:
      | variable           |
      | COURSE_GATEWAY_URL |
      | KB_MCP_TOKEN       |

  @R23
  Scenario: The older variable names still work
    Given a key with every example model
    And GATEWAY_URL and COURSE_MCP_TOKEN are set to the gateway and the key
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 0
    And the client sent 2 requests

  @R23
  Scenario: KB_MCP_TOKEN wins over COURSE_MCP_TOKEN
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And the variable COURSE_MCP_TOKEN is set to "a-token-the-gateway-does-not-know"
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 0

  @R30 @R32
  Scenario Outline: A refusal is printed with what to do, and is not tried again
    Given <key>
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-large --max-tokens 1000"
    Then the exit code is 1
    And the printed output holds "refused: HTTP <status> <code>"
    And the printed output holds "what to do: <advice>"
    And the client sent <requests> requests
    And the client did not wait

    Examples:
      | key                                   | status | code              | advice                                                    | requests |
      | a key with a budget of 0.0001         | 429    | budget_exhausted  | this request does not fit what is left of your budget     | 2        |
      | a key that was revoked                | 403    | key_revoked       | this token no longer works on the gateway                 | 1        |
      | a key that expired yesterday          | 403    | key_expired       | this token has passed its last day on the gateway         | 1        |
      | a key for the model example-small only | 403   | model_not_allowed | pick a model from the list above                          | 2        |
      | a key the gateway does not know       | 401    | invalid_api_key   | the token is missing or unknown: check KB_MCP_TOKEN       | 1        |

  @R31
  Scenario: A rate-limit refusal is waited out once, for the time the gateway names
    Given a key with a rate limit of 1 request a minute
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-small"
    And main/call_gateway.py runs with "--model example-small"
    Then the printed output holds "refused: HTTP 429 rate_limit_exceeded"
    And the printed output holds "what to do: too many requests this minute: wait for the next minute"
    And the client waited once, for 60 seconds
    And the client sent 5 requests

  @R31
  Scenario Outline: The wait is never longer than 65 seconds, and 5 seconds when the refusal names none
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And the model service answers every request with 429 <retry_after>
    When main/call_gateway.py runs with "--model example-small"
    Then the printed output holds "refused: HTTP 429 upstream_rate_limited"
    And the client waited once, for <seconds> seconds

    Examples:
      | retry_after           | seconds |
      | and Retry-After 600   | 65      |
      | and Retry-After 7     | 7       |
      | and no Retry-After    | 5       |

  @R33
  Scenario: A gateway address where nothing answers
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And COURSE_GATEWAY_URL is an address where nothing listens
    And every request the client sends is recorded
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 1
    And the printed output holds "no answer from the gateway: ConnectError"
    And the printed output holds "what to do: check COURSE_GATEWAY_URL (it ends in /v1) and your network, then run it again"
    And the error output is empty
    And the client did not wait
    And the client sent 1 request
    And the client's time limit for one request is 250 seconds

  @R33
  Scenario: A chat that gets no byte in time is not sent again
    Given a stand-in at COURSE_GATEWAY_URL that lists its models and takes a chat without answering it
    And the client's time limit is shortened to 0.3 seconds
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 1
    And the printed output holds "no answer from the gateway: ReadTimeout"
    And the error output is empty
    And the client did not wait
    And the stand-in received 1 chat

  @R33
  Scenario: An answer that is not JSON
    Given a stand-in at COURSE_GATEWAY_URL that answers every request with a web page
    When main/call_gateway.py runs with "--model example-small"
    Then the exit code is 1
    And the printed output holds "the answer was not JSON, so it may not have come from the gateway"
    And the printed output holds "what to do: check COURSE_GATEWAY_URL (it ends in /v1): another server may answer at that address"
    And the error output is empty
    And the stand-in received 1 request in all

  @R40 @R41
  Scenario: A run creates nothing and reaches nothing beyond this machine
    Given a key with every example model
    And COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key
    And every connection to an address other than 127.0.0.1 fails the scenario
    When main/call_gateway.py runs from an empty folder with "--model example-small"
    Then the exit code is 0
    And the folder is still empty

  @R15
  Scenario Outline: A chat sends temperature 0, except to a GPT model
    When the client builds the sampling settings for "<model>"
    Then they are <settings>

    Examples:
      | model           | settings           |
      | example-small   | {"temperature": 0} |
      | qwen3.8-27b     | {"temperature": 0} |
      | gpt-4o          | {}                 |
      | openai/gpt-4.1  | {}                 |
      | o4-mini         | {}                 |
