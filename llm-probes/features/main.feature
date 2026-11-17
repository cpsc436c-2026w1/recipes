Feature: Probe an OpenAI-compatible endpoint, the main scenario
  One small request per feature; a table that says which models answer and which features work.
  README, "Requirements": R10 to R19, R20 to R23, R30 to R39.

  Background:
    Given LLM_BASE_URL and LLM_API_KEY are set to the stand-in endpoint and its key

  @R10 @R11
  Scenario: The main group on an endpoint where everything works
    When main/probe.py runs with "--groups main"
    Then the exit code is 0
    And the rows are "list models, wrong key is refused, unknown model is refused, chat"
    And the row "list models" says yes with the note "fake-chat, fake-embed"
    And every row has works "yes", a time in seconds and a note
    And the first printed line names the host "endpoint.example" and not the path or the key

  @R10
  Scenario: Every group, in the order of the captured run
    When main/probe.py runs
    Then the exit code is 0
    And the rows are "list models, wrong key is refused, unknown model is refused, chat, streaming, usage in the stream, same reply on repeat, max_tokens stops the reply, tool call, tool result used, output fits a JSON schema, schema wins over the prompt, prompt cache reported, embeddings"
    And every row has works "yes", a time in seconds and a note

  @R12
  Scenario: An endpoint that returns no model list
    Given the endpoint answers GET /models with HTTP 500
    When main/probe.py runs
    Then the exit code is 1
    And the rows are "list models"
    And the row "list models" says no with a note that starts with "HTTP 500"
    And the endpoint received 1 request

  @R12
  Scenario: A key the endpoint does not know
    Given LLM_API_KEY is "some-other-key"
    When main/probe.py runs
    Then the exit code is 1
    And the rows are "list models"
    And the row "list models" says no with a note that starts with "HTTP 401"

  @R12
  Scenario: An endpoint whose model list is empty
    Given the endpoint answers GET /models with an empty list
    When main/probe.py runs
    Then the exit code is 1
    And the rows are "list models"
    And the row "list models" says no with the note "the endpoint lists no models for this key"
    And the endpoint received 1 request

  @R13
  Scenario Outline: The wrong-key row says yes only for a refusal
    Given the endpoint answers a made-up key with HTTP <status>
    When main/probe.py runs with "--groups main"
    Then the row "wrong key is refused" says <works>

    Examples:
      | status | works |
      | 401    | yes   |
      | 403    | yes   |
      | 200    | no    |
      | 500    | no    |

  @R14
  Scenario Outline: The unknown-model row says yes only for a 4xx answer
    Given the endpoint answers a model name it does not know with HTTP <status>
    When main/probe.py runs with "--groups main"
    Then the row "unknown model is refused" says <works>

    Examples:
      | status | works |
      | 404    | yes   |
      | 400    | yes   |
      | 401    | yes   |
      | 403    | yes   |
      | 200    | no    |
      | 500    | no    |

  @R15
  Scenario: A chat model that answers
    When main/probe.py runs with "--groups main"
    Then for "fake-chat" the row "chat" says yes with the note "replied 'ready'; 29 tokens in, 2 out"

  @R15
  Scenario: Reasoning tokens are counted in the note
    Given the endpoint answers the one-word chat with "ready", 30 tokens out, 26 of them reasoning
    When main/probe.py runs with "--groups main"
    Then for "fake-chat" the row "chat" says yes with the note "replied 'ready'; 29 tokens in, 30 out, 26 of them reasoning"

  @R16
  Scenario Outline: A chat model that does not answer is skipped for every other probe
    Given the endpoint answers the one-word chat with <answer>
    When main/probe.py runs
    Then for "fake-chat" the row "chat" says no
    And for "fake-chat" the row "other chat probes" says skipped
    And the rows are "list models, wrong key is refused, unknown model is refused, chat, other chat probes, embeddings"

    Examples:
      | answer         |
      | HTTP 500       |
      | an empty reply |

  @R17
  Scenario: A model with embed in its name gets no chat probe
    When main/probe.py runs
    Then no chat request named the model "fake-embed"
    And for "fake-embed" the row "embeddings" says yes

  @R18
  Scenario: --groups chooses the extensions
    When main/probe.py runs with "--groups main,tool-calls"
    Then the rows are "list models, wrong key is refused, unknown model is refused, chat, tool call, tool result used"

  @R18
  Scenario: --models chooses the models
    When main/probe.py runs with "--models fake-embed"
    Then the rows are "list models, wrong key is refused, unknown model is refused, embeddings"

  @R19
  Scenario: A group the recipe does not have
    When main/probe.py runs with "--groups main,vision"
    Then the exit code is 1
    And the error output holds "Unknown group vision"
    And the endpoint received 0 requests

  @R20 @R21
  Scenario: The key comes from the environment and travels only in the Authorization header
    When main/probe.py runs
    Then every request that carries the key carries it as "Authorization: Bearer" and nowhere else
    And every request went to an address that starts with "https://endpoint.example/v1/"
    And the request with the made-up key is the only one without the key

  @R20
  Scenario Outline: There is no flag for the key or the address
    When main/probe.py runs with "<flag>"
    Then the exit code is 2
    And the endpoint received 0 requests

    Examples:
      | flag                                   |
      | --api-key abc                          |
      | --base-url https://endpoint.example/v1 |

  @R20 @R23
  Scenario: A key or an address under another variable name is not used
    Given LLM_API_KEY is not set
    And LLM_BASE_URL is not set
    And OPENAI_API_KEY and OPENAI_BASE_URL are set to the stand-in endpoint and its key
    When main/probe.py runs
    Then the exit code is 1
    And the error output holds "LLM_API_KEY"
    And the endpoint received 0 requests

  @R22
  Scenario Outline: A long error that carries the key near the place where the note is cut
    Given the endpoint answers <request> with an error of 300 characters that has the key at character 100
    When main/probe.py runs with "--groups main,streaming --json" and a file in the temporary folder
    Then the row "<row>" has a note of at most 125 characters
    And no piece of the key of 8 characters or more is in a printed line or in the file

    Examples:
      | request                      | row                      |
      | a model name it does not know | unknown model is refused |
      | a streamed request           | streaming                |

  @R22
  Scenario: A streamed request's error body that is cut 7 characters into the key
    # The note keeps 110 characters of the body. Masked after the cut, the first 7 characters of the key stay:
    # too short a piece for the mask of the printed table to find.
    Given the endpoint answers a streamed request with an error body of plain text that has the key at character 103
    When main/probe.py runs with "--groups main,streaming --json" and a file in the temporary folder
    Then the row "streaming" has a note of at most 125 characters
    And the first 5 characters of the key are in no printed line and not in the file

  @R22
  Scenario: An error that echoes only the end of the key
    Given the endpoint answers a model name it does not know with an error that holds the last 10 characters of the key
    When main/probe.py runs with "--groups main --json" and a file in the temporary folder
    Then no piece of the key of 8 characters or more is in a printed line or in the file
    And the printed output holds "[key]"

  @R22
  Scenario: A key with a table character in it is masked as it came
    Given LLM_API_KEY is "wrong|key-with-a-bar-in-it"
    When main/probe.py runs
    Then the printed output holds "[key]"
    And the printed output does not hold "key-with-a-bar"

  @R22
  Scenario: A key shorter than 8 characters is not masked
    Given LLM_API_KEY is "short"
    When main/probe.py runs
    Then the printed output does not hold "[key]"

  @R22
  Scenario: An error message that echoes the key back is masked on the screen and in the file
    When main/probe.py runs with "--json" and a file in the temporary folder
    Then the exit code is 0
    And the key is in no printed line and not in the file
    And the printed output holds "[key]"
    And the file holds "[key]"

  @R23
  Scenario Outline: A run without its two variables
    Given <variable> is not set
    When main/probe.py runs
    Then the exit code is 1
    And the error output holds "LLM_BASE_URL"
    And the error output holds "LLM_API_KEY"
    And the endpoint received 0 requests

    Examples:
      | variable     |
      | LLM_BASE_URL |
      | LLM_API_KEY  |

  @R30
  Scenario: The time limit of one request
    When main/probe.py runs with "--groups main,streaming"
    Then the first printed line ends with "60 s timeout per request"
    And every request was sent with a time limit of 60 seconds for the reply

  @R30
  Scenario: --timeout names another limit
    When main/probe.py runs with "--groups main,streaming --timeout 20"
    Then the first printed line ends with "20 s timeout per request"
    And every request was sent with a time limit of 20 seconds for the reply

  @R31
  Scenario: A request that gets no reply in time
    Given the endpoint does not answer a made-up key in time
    When main/probe.py runs with "--groups main"
    Then the exit code is 0
    And the row "wrong key is refused" says no with a note that starts with "no reply (ReadTimeout)"
    And the rows are "list models, wrong key is refused, unknown model is refused, chat"

  @R31
  Scenario: A reply with status 200 that is not in the expected format
    Given the endpoint answers the one-word chat with status 200 and no choices
    When main/probe.py runs
    Then the exit code is 0
    And for "fake-chat" the row "chat" says no with the note "malformed reply (KeyError)"
    And for "fake-chat" the row "other chat probes" says skipped
    And for "fake-embed" the row "embeddings" says yes

  @R31
  Scenario: A stream chunk that is not JSON
    Given the endpoint streams a chunk that is not JSON
    When main/probe.py runs with "--groups main,streaming,reply-control"
    Then for "fake-chat" the row "stream" says no with the note "malformed reply (JSONDecodeError)"
    And for "fake-chat" the row "same reply on repeat" says yes

  @R32
  Scenario: Every chat request is small
    When main/probe.py runs
    Then every chat request has temperature 0
    And every chat request has max_tokens of 400 or less
    And the one-word chat asks for at most 200 tokens

  @R33
  Scenario: The probes only read
    When main/probe.py runs
    Then the only requests are "GET /v1/models, POST /v1/chat/completions, POST /v1/embeddings"

  @R34
  Scenario: --json also writes the rows to a file
    When main/probe.py runs with "--groups main --json" and a file in the temporary folder
    Then the file holds 4 rows with "probe, model, works, seconds, note"
    And the rows in the file are the rows that were printed

  @R35
  Scenario: Headers a proxy sends are shown under the table
    Given the endpoint's replies carry the header "x-litellm-key-spend: 0.0077" and the header "x-request-id: abc"
    When main/probe.py runs with "--groups main"
    Then the printed output holds "Headers on the last replies:"
    And the printed output holds "x-litellm-key-spend: 0.0077"
    And the printed output does not hold "x-request-id"

  @R35
  Scenario Outline: Each header the recipe shows
    Given the endpoint's replies carry the header "<header>: 7" and the header "x-request-id: abc"
    When main/probe.py runs with "--groups main"
    Then the printed output holds "  <header>: 7"

    Examples:
      | header                         |
      | server                         |
      | retry-after                    |
      | x-ratelimit-remaining-requests |
      | x-ratelimit-limit-tokens       |
      | x-litellm-version              |
      | x-litellm-attempted-retries    |
      | x-litellm-attempted-fallbacks  |
      | x-litellm-response-cost        |
      | x-litellm-key-spend            |
      | x-litellm-key-max-budget       |

  @R37
  Scenario: Every required row says yes
    When main/probe.py runs with "--groups main,tool-calls --require 'tool call,tool result used'"
    Then the exit code is 0
    And the printed output does not hold "required and not yes"

  @R37
  Scenario: A required row says no
    Given the endpoint does nothing right beyond listing models and chatting
    When main/probe.py runs with "--groups main,tool-calls,streaming --require 'tool call,streaming'"
    Then the exit code is 1
    And the last printed line is "required and not yes: tool call (fake-chat: no)"

  @R37
  Scenario: A required row was not run
    When main/probe.py runs with "--groups main --require 'tool call'"
    Then the exit code is 1
    And the last printed line is "required and not yes: tool call (not run)"

  @R37
  Scenario: A required row is missing for one of two chat models
    Given the endpoint lists the chat models "fake-chat" and "fake-chat-b"
    And the endpoint answers the one-word chat of "fake-chat-b" with HTTP 500
    When main/probe.py runs with "--groups main,tool-calls --require 'tool call'"
    Then the row "tool call" of "fake-chat" says "yes"
    And the exit code is 1
    And the last printed line is "required and not yes: tool call (fake-chat-b: not run)"

  @R37
  Scenario: A required row says no for one of two chat models
    Given the endpoint lists the chat models "fake-chat" and "fake-chat-b"
    And the endpoint answers "fake-chat-b" with plain text where a tool is offered
    When main/probe.py runs with "--groups main,tool-calls --require 'tool call'"
    Then the exit code is 1
    And the last printed line is "required and not yes: tool call (fake-chat-b: no)"

  @R37
  Scenario: A required probe of the embedding model is not owed by the chat models
    When main/probe.py runs with "--require 'embeddings,list models'"
    Then the exit code is 0

  @R37
  Scenario: Without --require, a row that says no does not change the exit code
    Given the endpoint does nothing right beyond listing models and chatting
    When main/probe.py runs
    Then the exit code is 0

  @R38
  Scenario Outline: The run says how many tokens the endpoint reported
    When main/probe.py runs with "--groups <groups>"
    Then the printed output holds "Tokens the endpoint reported for this run: <tokens>"

    Examples:
      | groups                                                                    | tokens         |
      | main                                                                      | 29 in, 2 out   |
      | main,streaming                                                            | 58 in, 4 out   |
      | main,embeddings                                                           | 50 in, 2 out   |
      | main,streaming,reply-control,tool-calls,structured-output,prompt-cache,embeddings | 340 in, 25 out |

  @R38
  Scenario: A reply that gives only the total and the output
    Given the endpoint answers the one-word chat with a usage of 31 tokens in total and 2 out, and no input count
    When main/probe.py runs with "--groups main"
    Then the printed output holds "Tokens the endpoint reported for this run: 29 in, 2 out"

  @R38
  Scenario: A stream without usage adds nothing to the total
    Given the endpoint does nothing right beyond listing models and chatting
    When main/probe.py runs with "--groups main,streaming"
    Then the printed output holds "Tokens the endpoint reported for this run: 29 in, 2 out"

  @R36
  Scenario: A run writes no file unless --json names one
    When main/probe.py runs from the temporary folder
    Then the temporary folder is empty
