Feature: Model calls go through the course LLM gateway
  README, "Requirements": R30 to R37.

  Background:
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values

  @R30 @R32 @R37
  Scenario: Embeddings: one model, batches of at most 32, each text once
    Given the gateway is a stand-in
    And 40 labelled queries, each asked once
    When main/run.py runs with "--embedder gateway" and those queries
    Then the exit code is 0
    And every request went to "https://gw.example/v1/embeddings" with the course token as the bearer key
    And every embedding request asks for "qwen3-embedding-0.6b"
    And the embedding requests held 30, 32 and 8 texts, 70 different texts in all
    And the course token is in no printed line and in no output file

  @R31
  Scenario: Chat calls: the cheaper model, temperature 0, at most 150 tokens
    Given the gateway is a stand-in
    When main/run.py runs with "--answers gateway"
    Then every chat call went to "https://gw.example/v1/chat/completions" with the course token as the bearer key
    And every chat call asks for "qwen3.6-35b-a3b" with temperature 0 and max_tokens 150

  @R33
  Scenario Outline: A rate-limit refusal is waited out as long as Retry-After says, then sent again
    Given the gateway is a stand-in that refuses the first call with HTTP 429 "<code>" and Retry-After "<retry_after>"
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 0
    And the recipe waited <wait> seconds once
    And the first embedding request was sent twice

    Examples:
      | code                  | retry_after | wait |
      | rate_limit_exceeded   | 7           | 7    |
      | upstream_rate_limited | 65          | 65   |

  @R33
  Scenario: Retry-After as an HTTP date is read as the seconds until then
    Given the gateway is a stand-in that refuses the first call with HTTP 429 "rate_limit_exceeded" and a Retry-After date 30 seconds ahead
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 0
    And the recipe waited between 28 and 30 seconds once

  @R34
  Scenario: A fourth rate-limit refusal of one request stops the run
    Given the gateway is a stand-in that refuses the first 4 calls with HTTP 429 "rate_limit_exceeded" and Retry-After "1"
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "HTTP 429 rate_limit_exceeded"
    And the gateway received 4 requests

  @R34
  Scenario Outline: Any other refusal stops the run at once
    Given the gateway is a stand-in that refuses the first call with HTTP <status> "<code>" and Retry-After "<retry_after>"
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "HTTP <status> <code>"
    And the gateway received 1 request

    Examples:
      | status | code                | retry_after |
      | 429    | budget_exhausted    | 60          |
      | 429    | rate_limit_exceeded | 66          |
      | 401    | invalid_key         | 0           |
      | 403    | key_revoked         | 0           |
      | 500    | upstream_error      | 0           |

  @R35
  Scenario Outline: A gateway run without the address or the token stops before any request
    Given <variable> is not set
    And the gateway is a stand-in
    When main/run.py runs with "<options>"
    Then the exit code is 1
    And the run stopped with "set COURSE_GATEWAY_URL and KB_MCP_TOKEN first"
    And the gateway received 0 requests

    Examples:
      | variable         | options             |
      | GATEWAY_URL      | --embedder gateway  |
      | COURSE_MCP_TOKEN | --embedder gateway  |
      | GATEWAY_URL      | --answers gateway   |
      | COURSE_MCP_TOKEN | --answers gateway   |

  @R36
  Scenario: A reply with fewer embeddings than texts stops the run
    Given the gateway is a stand-in that returns one embedding fewer than it was sent
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "the gateway returned 29 embeddings for 30 texts"

  @R37
  Scenario: A refusal whose message holds the course token does not print it
    Given the gateway is a stand-in that refuses the first call with HTTP 401 and a message that holds the key
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "HTTP 401 invalid_key"
    And the course token is in no printed line and in no output file
  @R38
  Scenario Outline: An answer with status 200 that is not what was asked stops the run with one line
    Given the gateway is a stand-in that answers the <path> calls with status 200 and <answer>
    When main/run.py runs with "--embedder gateway --answers gateway"
    Then the exit code is 1
    And the run stopped with "<says>"
    And no line of the output is a Python traceback

    Examples:
      | path       | answer                  | says                     |
      | embeddings | a page that is not JSON | not_an_answer            |
      | embeddings | an error object         | not_an_answer            |
      | embeddings | no list                 | not_an_embedding_list    |
      | chat       | a page that is not JSON | not_an_answer            |
      | chat       | no list                 | not_a_chat_completion    |

  @R39
  Scenario: A connection to the gateway that fails stops the run with one line, and the request is not sent again
    Given the gateway is a stand-in whose connection fails
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "no_connection"
    And no line of the output is a Python traceback
    And the gateway received 1 request

  @R39
  Scenario: A gateway that does not answer in time stops the run, and the request is not sent again
    Given the gateway is a stand-in that does not answer within the time limit
    When main/run.py runs with "--embedder gateway"
    Then the exit code is 1
    And the run stopped with "no_answer"
    And the gateway received 1 request
    And that request carried a read time limit of 60 seconds
