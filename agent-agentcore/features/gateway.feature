Feature: The tools are behind an AgentCore Gateway
  Offline: moto stands in for Lambda and DynamoDB, tests/fakes.py for the AgentCore API.

  @R50
  Scenario: The deploy creates the function, the Gateway and one Lambda target
    Given a stand-in for the team sandbox
    And the course token and the gateway address are set
    When the deploy runs
    Then there is a Gateway with protocol "MCP" and inbound authorization "AWS_IAM"
    And its one target points at the tools function and lists the tools "search_books, get_book, get_holds, place_hold"

  @R51
  Scenario: The function reads the tool's name from the client context
    Given the recipe is deployed
    When the Gateway invokes the tools function for "search_books" with '{"query": "austen"}'
    Then the function returns the books "b02, b03"

  @R51
  Scenario: A tool the function does not have is an error result
    Given the recipe is deployed
    When the Gateway invokes the tools function for "drop_table" with '{}'
    Then the result is an error that says "unknown tool"

  @R52
  Scenario: The hooks apply to the names the Gateway gives the tools
    Given the tools are behind a stand-in for the Gateway
    And the stub model behaves "wrong_member"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then 1 approvals were asked
    And the Gateway got the write with member "sam" and a request key
    And member "alex" has no holds
