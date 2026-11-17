Feature: One settings object, read from the environment

  @R70
  Scenario: The settings object mirrors .env.example and cannot be changed
    When the settings are read
    Then the settings object has a field for every line of .env.example and no other
    And the settings object has "max_model_calls" equal to "8"
    And a change to the settings object is refused

  @R71
  Scenario Outline: A setting that is not set stops the run and is named
    Given the setting "<name>" is not set
    When the settings are read
    Then the run stops and names "<name>"

    Examples:
      | name            |
      | PREFIX          |
      | AGENT_MODEL     |
      | MAX_MODEL_CALLS |
      | LOGFIRE_ENABLED |

  @R71
  Scenario: The deploy asks for the course token before its first AWS call
    Given a stand-in for the team sandbox
    And the setting "COURSE_GATEWAY_URL" is "https://llm.test.invalid/v1"
    And the setting "KB_MCP_TOKEN" is empty
    When the deploy runs
    Then the run stops and names "KB_MCP_TOKEN"
    And no AWS call was made

  @R72
  Scenario: Resource names are built from PREFIX
    Given the setting "PREFIX" is "recipe-ana"
    When the settings are read
    Then every resource name is built from "recipe-ana"

  @R73
  Scenario: With --model gateway the model calls go to the course LLM gateway with the course token
    Given a local stand-in for the course LLM gateway
    When the command "ask --model gateway --yes --request Place_a_hold_on_Emma." runs
    Then the exit code is 0
    And the output has "done: 3 model calls, 2 tool calls"
    And the stand-in got 3 requests, each with the course token and the model "qwen3.8-27b"

  @R73
  Scenario: The older names of the token and the address still work
    Given the setting "KB_MCP_TOKEN" is not set
    And the setting "COURSE_GATEWAY_URL" is not set
    And the older name "COURSE_MCP_TOKEN" is "old-token"
    And the older name "GATEWAY_URL" is "http://old.invalid/v1"
    When the settings are read
    Then the settings object has "kb_mcp_token" equal to "old-token"
    And the settings object has "course_gateway_url" equal to "http://old.invalid/v1"
