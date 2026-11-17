Feature: The capability groups
  One folder per capability; each row says yes only when the feature really worked.
  The extension READMEs, "Requirements": R40 to R42, R50 to R52, R60 to R62, R70 and R71, R80 and R81, R90 and R91.

  Background:
    Given LLM_BASE_URL and LLM_API_KEY are set to the stand-in endpoint and its key

  @R40 @R41
  Scenario: A streamed reply with usage in its last chunk
    When main/probe.py runs with "--groups main,streaming"
    Then for "fake-chat" the row "streaming" says yes with a note that starts with "first token after"
    And for "fake-chat" the row "usage in the stream" says yes with the note "29 tokens in, 2 out"
    And the streamed request asked for stream true and usage in the stream

  @R41
  Scenario: A stream without usage
    Given the endpoint does nothing right beyond listing models and chatting
    When main/probe.py runs with "--groups main,streaming"
    Then for "fake-chat" the row "streaming" says yes
    And for "fake-chat" the row "usage in the stream" says no with the note "no usage in the reply"

  @R40
  Scenario: Reasoning chunks before the text are counted
    Given the endpoint streams 3 reasoning chunks and then the text
    When main/probe.py runs with "--groups main,streaming"
    Then for "fake-chat" the row "streaming" says yes with a note that ends with "3 reasoning chunks came first"

  @R42
  Scenario Outline: A streamed request that fails
    Given the endpoint answers a streamed request with <answer>
    When main/probe.py runs with "--groups main,streaming,reply-control"
    Then for "fake-chat" the row "streaming" says no with a note that starts with "<note>"
    And there is no row "usage in the stream"
    And for "fake-chat" the row "same reply on repeat" says yes

    Examples:
      | answer        | note                   |
      | HTTP 400      | HTTP 400               |
      | no reply      | no reply (ReadTimeout) |

  @R50
  Scenario: The same reply twice at temperature 0
    When main/probe.py runs with "--groups main,reply-control"
    Then for "fake-chat" the row "same reply on repeat" says yes

  @R50
  Scenario: Two different replies at temperature 0
    Given the endpoint answers the one-word chat with a different word each time
    When main/probe.py runs with "--groups main,reply-control"
    Then for "fake-chat" the row "same reply on repeat" says no

  @R51
  Scenario Outline: max_tokens stops the reply
    Given the endpoint answers the request with max_tokens 5 with finish_reason "<finish>" and <out> tokens out
    When main/probe.py runs with "--groups main,reply-control"
    Then for "fake-chat" the row "max_tokens stops the reply" says <works>
    And the request that counts to 50 has max_tokens 5

    Examples:
      | finish | out | works |
      | length | 5   | yes   |
      | length | 6   | no    |
      | stop   | 5   | no    |
      | stop   | 40  | no    |

  @R52
  Scenario: A reasoning model that spends the whole limit on reasoning
    Given the endpoint answers the request with max_tokens 5 with an empty reply, finish_reason "length" and 5 tokens out
    When main/probe.py runs with "--groups main,reply-control"
    Then for "fake-chat" the row "max_tokens stops the reply" says yes with the note "finish_reason length, 5 tokens out, all of them reasoning: the reply text is empty"

  @R60 @R61
  Scenario: A tool call, then the tool result in the reply
    When main/probe.py runs with "--groups main,tool-calls"
    Then for "fake-chat" the row "tool call" says yes with the note "run_sql('SELECT COUNT(*) FROM trips')"
    And for "fake-chat" the row "tool result used" says yes
    And the second tool request carries the tool result "48213" for the call "call-1"

  @R61
  Scenario: A reply that does not use the tool result
    Given the endpoint answers the request that carries a tool result with "The table has many rows."
    When main/probe.py runs with "--groups main,tool-calls"
    Then for "fake-chat" the row "tool call" says yes
    And for "fake-chat" the row "tool result used" says no

  @R62
  Scenario Outline: No usable tool call
    Given the endpoint answers the request that offers a tool with <answer>
    When main/probe.py runs with "--groups main,tool-calls"
    Then for "fake-chat" the row "tool call" says no
    And there is no row "tool result used"
    And 1 request offered a tool

    Examples:
      | answer                                   |
      | plain text                               |
      | a tool call whose arguments are not JSON |
      | a tool call without the query argument   |

  @R70
  Scenario Outline: The reply fits the JSON schema, or it does not
    Given the endpoint answers a request with a response format with the text <text>
    When main/probe.py runs with "--groups main,structured-output"
    Then for "fake-chat" the row "output fits a JSON schema" says <works>
    And every request with a response format sends the strict schema with city and population

    Examples:
      | text                                                   | works |
      | {"city": "Vancouver", "population": 660000}            | yes   |
      | {"city": "Vancouver", "population": "660 thousand"}    | no    |
      | {"city": "Vancouver", "population": 660000, "x": 1}    | no    |
      | {"city": "Vancouver"}                                  | no    |
      | Vancouver has 660 thousand residents.                  | no    |

  @R71
  Scenario: The second request asks for prose, against the schema
    When main/probe.py runs with "--groups main,structured-output"
    Then for "fake-chat" the row "schema wins over the prompt" says yes
    And the last request with a response format asks for "one plain English sentence, not JSON"

  @R80 @R81
  Scenario: Cached input tokens reported on the second call
    When main/probe.py runs with "--groups main,prompt-cache"
    Then for "fake-chat" the row "prompt cache reported" says yes with a note that starts with "second call: 3000 of 29 input tokens cached"
    And 2 requests carried the same system prompt of more than 10000 characters, with the question last

  @R80
  Scenario Outline: No cached tokens reported
    Given the endpoint answers the long prompt with <cached> cached tokens
    When main/probe.py runs with "--groups main,prompt-cache"
    Then for "fake-chat" the row "prompt cache reported" says no

    Examples:
      | cached |
      | no     |
      | 0      |

  @R90 @R91
  Scenario: A similar pair closer than an unrelated pair
    When main/probe.py runs with "--groups main,embeddings"
    Then for "fake-embed" the row "embeddings" says yes with the note "2 dimensions; similar pair 0.99, unrelated pair 0.00"
    And the embeddings request sent 3 sentences to POST /embeddings

  @R90
  Scenario: An unrelated pair closer than the similar pair
    Given the endpoint does nothing right beyond listing models and chatting
    When main/probe.py runs with "--groups main,embeddings"
    Then for "fake-embed" the row "embeddings" says no

  @R90
  Scenario: A similar pair that is close, and an unrelated pair that is closer still
    Given the endpoint answers the embeddings request with the vectors [[1, 0], [0.6, 0.8], [0.9, 0.1]]
    When main/probe.py runs with "--groups main,embeddings"
    Then for "fake-embed" the row "embeddings" says no with the note "2 dimensions; similar pair 0.60, unrelated pair 0.99"
