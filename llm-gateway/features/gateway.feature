Feature: The gateway itself: a key per caller, a budget per key
  ext-deploy/README.md, "Requirements": R50 to R55, R60 to R69, R70 and R71, R80 and R81, R90 to R97.
  Every request goes over HTTP to the gateway's own code on 127.0.0.1. The fake upstream counts 18 input tokens
  for the one-message chat "hi" and 3 output tokens unless a scenario says otherwise. Prices of example-large:
  0.10 per million input tokens and 0.20 per million output, so 1 micro-unit is 0.000001.

  @R50
  Scenario Outline: A request without a key the gateway knows
    When a chat is sent with <key>
    Then the gateway answers 401 with the code "invalid_api_key"
    And the upstream received 0 requests

    Examples:
      | key                                |
      | no Authorization header            |
      | a key of another shape             |
      | a well-formed key that was never issued |

  @R51
  Scenario Outline: A key that no longer works
    Given <key>
    When the key sends a chat
    Then the gateway answers 403 with the code "<code>"
    And the upstream received 0 requests

    Examples:
      | key                          | code        |
      | a key that was revoked       | key_revoked |
      | a key that expired yesterday | key_expired |

  @R52
  Scenario: A model outside the key's list
    Given a key for the model example-small only
    When the key sends a chat
    Then the gateway answers 403 with the code "model_not_allowed"
    And the upstream received 0 requests

  @R53
  Scenario: Only the hash of a key is stored
    Given a key with every example model
    Then gw-admin issue printed the key once
    And the keys table holds the SHA-256 of the key and not the key
    And gw-admin list does not print the key

  @R54
  Scenario: The models a key may call
    Given a key for the model example-small only
    When the key asks for its models
    Then the gateway answers 200
    And the models listed are "example-small"
    And the upstream received 0 requests

  @R60 @R61
  Scenario Outline: The reserve is the highest price the request can reach
    Given a key with a budget of <budget>
    When the key sends a chat with max_tokens 1000
    Then the gateway answers <status>
    And the upstream received <forwarded> requests

    Examples: 18 input tokens (2 bytes and 16 for the message) are 2 micro-units, 1000 output tokens are 200
      | budget   | status | forwarded |
      | 0.000202 | 200    | 1         |
      | 0.000201 | 429    | 0         |

  @R60
  Scenario Outline: One more byte of input is one more token in the reserve
    Given a key with a budget of <budget>
    When the key sends a chat of <bytes> bytes with max_tokens 1000
    Then the gateway answers <status>

    Examples: 984 bytes and 16 for the message are 1,000 input tokens, 100 micro-units; 1,000 output tokens are 200
      | bytes | budget   | status |
      | 984   | 0.000300 | 200    |
      | 984   | 0.000299 | 429    |
      | 985   | 0.000300 | 429    |
      | 985   | 0.000301 | 200    |

  @R61
  Scenario: A reserve that does not fit
    Given a key with a budget of 0.0001
    When the key sends a chat with max_tokens 1000
    Then the gateway answers 429 with the code "budget_exhausted"
    And the reply has no Retry-After header
    And the key's spend is 0 micro-units

  @R62 @R66
  Scenario: After the reply the key is charged for the tokens it used
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000
    Then the gateway answers 200
    And the key's spend is 3 micro-units
    And the usage table has one row for the key, today (UTC) and "example-large": 1 request, 18 tokens in, 3 out, 3 micro-units

  @R62
  Scenario: A longer reply costs more
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream writes 500 tokens
    Then the key's spend is 102 micro-units

  @R95
  Scenario: A reply that is not streamed names what the key was charged
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream writes 500 tokens
    Then the reply has the header "x-gateway-cost-micro" with "102"
    And the key's spend is 102 micro-units

  @R95
  Scenario Outline: No charge is named where it is not known when the headers are sent
    Given a key with a budget of 1
    When the key sends <request>
    Then the gateway answers 200
    And the reply has no "x-gateway-cost-micro" header

    Examples:
      | request                                   |
      | a streamed chat                           |
      | a chat whose reply carries no usage       |

  @R63
  Scenario: A chat without max_tokens is forwarded with 4,096
    Given a key with a budget of 1
    When the key sends a chat without max_tokens
    Then the upstream received a chat with max_tokens 4096

  @R63
  Scenario: A chat that names only max_completion_tokens gets no max_tokens beside it
    Given a key with a budget of 1
    When the key sends a chat with max_completion_tokens 300 and no max_tokens
    Then the upstream received a chat with max_completion_tokens 300 and no max_tokens
    And the key's spend is 3 micro-units

  @R63
  Scenario: Without max_tokens, a key with a lower cap gets its cap
    Given a key with a budget of 1 and a max_tokens cap of 500
    When the key sends a chat without max_tokens
    Then the upstream received a chat with max_tokens 500

  @R55
  Scenario: max_tokens is clipped to the key's cap
    Given a key with a budget of 1 and a max_tokens cap of 500
    When the key sends a chat with max_tokens 9000
    Then the upstream received a chat with max_tokens 500

  @R55
  Scenario: max_completion_tokens is clipped to the key's cap
    Given a key with a budget of 1 and a max_tokens cap of 500
    When the key sends a chat with max_completion_tokens 9000 and no max_tokens
    Then the upstream received a chat with max_completion_tokens 500 and no max_tokens

  @R64
  Scenario Outline: A request the upstream turned away costs nothing
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream answers <status>
    Then the gateway answers <answer>
    And the key's spend is 0 micro-units

    Examples:
      | status | answer |
      | 400    | 400    |
      | 404    | 400    |
      | 503    | 502    |
      | 529    | 502    |

  @R64
  Scenario: A request the upstream never received costs nothing
    Given a key with a budget of 1
    And the upstream's address is one where nothing listens
    When the key sends a chat with max_tokens 1000
    Then the gateway answers 502 with the code "upstream_error"
    And the key's spend is 0 micro-units

  @R65
  Scenario Outline: A request the upstream had, and then failed, keeps the whole reserve
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream answers <status>
    Then the gateway answers 502 with the code "upstream_error"
    And the key's spend is 202 micro-units

    Examples:
      | status |
      | 500    |
      | 502    |

  @R65
  Scenario: A request the upstream had, and did not answer in time, keeps the whole reserve
    Given a key with a budget of 1
    And the gateway waits 0.2 seconds for the upstream
    When the key sends a chat with max_tokens 1000 and the upstream takes 1 second
    Then the gateway answers 502 with the code "upstream_error"
    And the key's spend is 202 micro-units

  @R68
  Scenario: A reply that is not streamed and reports no usage keeps the whole reserve
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream reports no usage
    Then the gateway answers 200
    And the key's spend is 202 micro-units
    And the usage row of the key counts 1 request as estimated

  @R69
  Scenario: A stream that ends without a usage chunk is charged the input estimate and one token per chunk
    Given a key with a budget of 1
    When the key sends a streamed chat and the upstream sends 12 chunks and no usage
    Then the gateway answers 200
    And the key's spend is 5 micro-units
    And the usage table has one row for the key, today (UTC) and "example-large": 1 request, 18 tokens in, 12 out, 5 micro-units
    And the usage row of the key counts 1 request as estimated

  @R69
  Scenario: A stream that breaks is charged the chunks that arrived, not the reserve
    Given a key with a budget of 1
    When the key sends a streamed chat with max_tokens 1000 and the upstream breaks after 7 of 12 chunks
    Then the key's spend is 4 micro-units
    And the usage row of the key counts 1 request as estimated

  @R69
  Scenario: A stream that stalls is charged the chunks that arrived, not the reserve
    Given a key with a budget of 1
    And the gateway waits 0.2 seconds for the upstream
    When the key sends a streamed chat with max_tokens 1000 and the upstream waits 1 second after each of 12 chunks
    Then the key's spend is 3 micro-units
    And the usage row of the key counts 1 request as estimated

  @R67
  Scenario Outline: A request the gateway cannot bound is refused before any reserve
    Given a key with a budget of 1
    When the key sends <request>
    Then the gateway answers <status> with the code "<code>"
    And the key's spend is 0 micro-units
    And the upstream received 0 requests

    Examples:
      | request                         | status | code                  |
      | a body that is not JSON         | 400    | bad_json              |
      | a JSON list as the body         | 400    | bad_json              |
      | a chat with n set to 2          | 400    | n_not_supported       |
      | a chat with best_of             | 400    | best_of_not_supported |
      | a chat of 300,000 bytes         | 413    | body_too_large        |

  @R70 @R71
  Scenario: More requests in a minute than the key's rate limit
    Given a key with a rate limit of 2 requests a minute
    When the key sends a chat
    And the key sends a chat
    And the key sends a chat
    Then the gateway answers 429 with the code "rate_limit_exceeded"
    And the reply has the header "Retry-After" with "60"
    And the upstream received 2 requests
    And the key's spend is 6 micro-units

  @R80
  Scenario: One notice for each threshold a key passes
    Given a key with a budget of 0.002008
    When the key sends 4 chats that each cost 502 micro-units
    Then the notices for the key's owner are "at 50% of budget, at 80% of budget, at 100% of budget"

  @R81
  Scenario: A request refused on budget publishes the last notice, once
    Given a key with a budget of 0.001
    When the key sends 6 chats that each cost 202 micro-units
    Then the answers were "200, 200, 200, 200, 429, 429"
    And the notices for the key's owner are "at 50% of budget, at 80% of budget, budget used up (request refused)"

  @R90
  Scenario: The provider key goes upstream; the caller's key does not
    Given a key with a budget of 1
    When the key sends a chat
    Then the upstream received the provider key as its bearer key
    And the caller's key is in nothing the upstream received
    And the provider key is in nothing the caller received

  @R91
  Scenario: A streamed reply is passed on unchanged, with usage asked for
    Given a key with a budget of 1
    When the key sends a streamed chat
    Then the gateway answers 200
    And the bytes the caller received are the bytes the upstream sent
    And the upstream was asked for usage in the stream
    And the key's spend is 3 micro-units

  @R92
  Scenario: The upstream's own rate limit
    Given a key with a budget of 1
    When the key sends a chat and the upstream answers 429 with Retry-After 7
    Then the gateway answers 429 with the code "upstream_rate_limited"
    And the reply has the header "Retry-After" with "7"
    And the key's spend is 0 micro-units

  @R93
  Scenario: The upstream's own error message does not reach the caller
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream answers 500
    Then the gateway answers 502 with the code "upstream_error"
    And the reply does not hold "internal upstream detail"

  @R96
  Scenario: A request the provider refused gets the provider's message back
    Given a key with a budget of 1
    When the key sends a chat and the upstream answers 400 with the message "temperature 0 is not supported"
    Then the gateway answers 400 with the code "upstream_rejected"
    And the reply's error message is "temperature 0 is not supported"
    And the key's spend is 0 micro-units

  @R97
  Scenario: The provider refused the gateway's own key
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000 and the upstream answers 403
    Then the gateway answers 502 with the code "upstream_key_refused"
    And the key's spend is 0 micro-units

  @R94
  Scenario: One log line per request, with the key id and nothing private
    Given a key with a budget of 1
    When the key sends a chat that says "a private question about salaries"
    Then the log has a line with the key id, "model=example-large status=200 tokens_in=", "tokens_out=3" and "cost_micro="
    And no log line holds the key, the owner's name or "salaries"

  @R94
  Scenario: A refusal is logged with its code
    When a chat is sent with a well-formed key that was never issued
    Then the log has the line "INFO refused request_id=<id> key_id=- status=401 code=invalid_api_key"
