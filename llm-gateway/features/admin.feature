Feature: The text log, gw-admin, the template and the clean-up
  ext-deploy/README.md, "Requirements": R100 and R101, R110 to R115, R120 and R121.

  @R100
  Scenario: While the text log is off, no text is stored
    Given a key with a budget of 1
    And the text log is off
    When the key sends a chat that says "a private question about salaries"
    Then the gateway answers 200
    And the texts table has no item for the key

  @R101
  Scenario: While the text log is on, a forwarded request is stored with its reply
    Given a key with a budget of 1
    And the text log is on
    When the key sends a chat that says "a question to keep"
    Then the texts table has one item for the key and the request id of the reply
    And the item holds the request as it went upstream and the reply "bonjour"

  @R101
  Scenario: While the text log is on, a refused request stores nothing
    Given a key with a budget of 0.0001
    And the text log is on
    When the key sends a chat with max_tokens 1000
    Then the gateway answers 429
    And the texts table has no item for the key

  @R110
  Scenario: A revoked key is refused
    Given a key with a budget of 1
    When gw-admin revokes the key
    And the key sends a chat
    Then the gateway answers 403 with the code "key_revoked"

  @R111
  Scenario: The spend report
    Given a key with a budget of 1
    When the key sends a chat with max_tokens 1000
    And gw-admin spend runs for the key
    Then the printed output has a line with the key id, today (UTC), "example-large  requests 1  in 18  out 3  0.000003 USD"
    And the printed output has a line "total", the key id and "0.000003 USD"

  @R112
  Scenario: A raised budget lets the key through again
    Given a key with a budget of 0.0001
    When the key sends a chat with max_tokens 1000
    Then the gateway answers 429
    When gw-admin sets the key's budget to 1
    And the key sends a chat with max_tokens 1000
    Then the gateway answers 200

  @R113
  Scenario: A key that another system made is accepted by its hash
    Given a key that another system made, imported by its SHA-256 for the owner "t07/ana"
    When the key sends a chat
    Then the gateway answers 200
    And gw-admin import printed counts and neither the owner nor the hash
    And the imported key's id starts with "im-"

  @R114
  Scenario: gw-admin kill closes the URL
    When gw-admin runs "kill"
    Then the function's reserved concurrency is 0

  @R115
  Scenario: gw-admin open sets it back
    When gw-admin runs "kill"
    And gw-admin runs "open"
    Then the function's reserved concurrency is 200

  @R120
  Scenario: The template creates nothing in IAM
    Then no resource of infra/template.yaml has a type that starts with "AWS::IAM::"
    And the template takes the function's role as the parameter "FunctionRoleArn"

  @R121
  Scenario: The clean-up commands name everything the deploy creates
    Then the README's clean-up block deletes the stack "llm-gateway"
    And it deletes the tables "llm-gateway-keys, llm-gateway-usage, llm-gateway-texts"
    And it deletes the secret "llm-gateway/upstream-key" without a recovery window
    And it deletes the role "llm-gateway-fn" and its inline policy
    And it deletes the inline policy "llm-gateway-deploy" that the deploy steps put on the role you work with
    And the README says that a table that was never created answers "ResourceNotFoundException"
