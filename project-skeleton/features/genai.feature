Feature: Generative AI first: a model that reads the table through one read-only tool
  rep-genai-first/run.py with --llm gateway. The course LLM gateway is a stand-in: the scenario states what the
  model answers, and no model is called (features/support.py). rep-genai-first/README.md, "Requirements": R70 to R80.

  Background:
    Given the gateway settings COURSE_GATEWAY_URL "https://gateway.example/v1" and KB_MCP_TOKEN "token-of-ana"

  @R70
  Scenario: What reaches the model
    Given the model calls count_updates for the window and then replies "There were {result} updates in the last hour."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the gateway client was made with the URL "https://gateway.example/v1" and the token "token-of-ana"
    And the system prompt names the window "2026-11-12T15:00:00.000Z" to "2026-11-12T16:00:00.000Z"
    And the user message is "How many updates arrived in the last hour?"
    And the model was offered the tools "count_updates"
    And every model call asked for the model "qwen3.6-35b-a3b" with at most 400 output tokens
    And the output holds "4. reply after 2 model calls (240 tokens in, 30 out): There were 143 updates in the last hour."
    And the output holds "5. reply states a number the tool returned: yes; the API's number is 143"

  @R79
  Scenario: A model that reasons before it answers gets room for the reasoning
    Given the model calls count_updates for the window and then replies "There were {result} updates in the last hour."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z --model qwen3.8-27b"
    Then the exit code is 0
    And every model call asked for the model "qwen3.8-27b" with at most 600 output tokens

  @R80
  Scenario: A reply that used its token cap before any text
    Given the model calls count_updates for the window and then replies with no text and finish_reason "length"
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z --model qwen3.8-27b"
    Then the exit code is 1
    And the output holds "4. no reply after 2 model calls (240 tokens in, 615 out): error: the model used its token cap before answering"
    And no printed line starts with "5. reply states"
    And no page for users was written

  @R80
  Scenario: A tool call cut at the token cap is not run and not tried again
    Given the model calls count_updates with arguments cut at the token cap, and then replies "There were 143 updates."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z --model qwen3.8-27b"
    Then the exit code is 1
    And the model was called 1 times
    And the output holds "4. no reply after 1 model calls (120 tokens in, 600 out): error: the model used its token cap before answering"
    And after the table was built, no DynamoDB call was made
    And no page for users was written

  @R71
  Scenario: The tool only reads minute counters
    Given the model calls count_updates for the window and then replies "There were {result} updates in the last hour."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then after the table was built, the only DynamoDB calls are BatchGetItem, of keys that start with "minute#"
    And the policy for a deployed tool allows "dynamodb:BatchGetItem" on the table "recipe-ana-updates" and nothing else

  @R72
  Scenario Outline: A window the tool refuses
    Given the model calls count_updates with start "<start>" and end "<end>", and then replies "I could not read it."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the tool result the model got is an error that holds "<says>"
    And after the table was built, no DynamoDB call was made
    And the output holds "5. reply states a number the tool returned: NO"

    Examples:
      | start                    | end                      | says                 |
      | 2026-11-12T15:00:30.000Z | 2026-11-12T16:00:00.000Z | use whole minutes    |
      | 2026-11-12T16:00:00.000Z | 2026-11-12T15:00:00.000Z | end must be after    |
      | 2026-11-10T15:00:00.000Z | 2026-11-12T16:00:00.000Z | at most 1440 minutes |
      | last hour                | now                      | use the form         |

  @R73
  Scenario: A model that keeps calling the tool
    Given the model calls count_updates for the window in every answer
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the model was called 3 times
    And the output holds "Stopped after 3 model calls without a reply."
    And the output holds "5. reply states a number the tool returned: NO"

  @R74
  Scenario: A reply with a number the tool did not return
    Given the model calls count_updates for the window and then replies "There were 999 updates in the last hour."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the output holds "5. reply states a number the tool returned: NO"
    And no page for users was written

  @R72
  Scenario: A model that names another tool
    Given the model calls the tool "delete_updates" and then replies "Done."
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the tool result the model got is an error that holds "no tool named delete_updates"
    And after the table was built, no DynamoDB call was made
    And no page for users was written

  @R78
  Scenario: DynamoDB keeps leaving the tool's counters unread
    Given the model calls count_updates with start "2026-11-12T15:00:00.000Z" and end "2026-11-12T16:00:00.000Z", and then replies "I could not read it."
    And after the table was built, DynamoDB leaves the counters of every read unread
    When rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the tool result the model got is an error that holds "the table is busy"
    And after the table was built, the tool asked DynamoDB for the counters 5 times

  @R75
  Scenario Outline: A gateway setting is missing
    Given the setting <setting> is "not set"
    When rep-genai-first/run.py runs with "--llm gateway"
    Then the exit code is 1
    And the output holds "set COURSE_GATEWAY_URL and KB_MCP_TOKEN"
    And no gateway client was made

    Examples:
      | setting            |
      | COURSE_GATEWAY_URL |
      | KB_MCP_TOKEN       |

  @R75
  Scenario: The older setting names still work, and KB_MCP_TOKEN wins over COURSE_MCP_TOKEN
    Given the setting COURSE_GATEWAY_URL is "not set"
    And the setting GATEWAY_URL is "https://older.example/v1"
    And the setting COURSE_MCP_TOKEN is "an-older-token"
    And the model calls count_updates for the window and then replies "There were {result} updates in the last hour."
    When rep-genai-first/run.py runs with "--llm gateway --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the gateway client was made with the URL "https://older.example/v1" and the token "token-of-ana"

  @R76
  Scenario: The assistant over the sandbox table
    Given Athena answers the count with 143, after scanning 9366 bytes
    And the model calls count_updates for the window and then replies "There were {result} updates in the last hour."
    When rep-operational-first/run.py runs with "--target aws --skip-export --as-of 2026-11-12T16:00:30Z"
    And rep-genai-first/run.py runs with "--target aws --llm gateway --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the output holds "tool result 143"
    And the genai run created nothing and wrote nothing to the table

  @R77
  Scenario: The same question of the lake, without a model
    Given Athena answers the count with 143, after scanning 9366 bytes
    When rep-operational-first/run.py runs with "--target aws --as-of 2026-11-12T16:00:30Z"
    And rep-genai-first/lake_example.py runs with "--target aws --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the view "events" was made over the export, and the count was asked of it
    And the output holds "updates in the last hour: lake 143, table 143"
    And no model was called
