Feature: Tool contracts are pydantic models

  @R40
  Scenario: The Gateway's tool definitions come from the argument models
    When the tool definitions for the Gateway are built
    Then each definition has the fields of the tool's pydantic model
    And the definitions pass the parameter check of the AgentCore API

  @R41
  Scenario: Arguments that do not match the model come back to the LLM and the tool does not run
    Given the stub model behaves "wrong_arguments"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then a tool call was refused with "q: Extra inputs are not permitted"
    And the tools that ran are "search_books, search_books, place_hold"
    And the status is "done"

  @R42
  Scenario Outline: A tool's result has the shape of its result model
    When the tool "<tool>" is called with '<arguments>'
    Then the result has the shape of the tool's result model

    Examples:
      | tool         | arguments                                                |
      | search_books | {"query": "austen", "filters": {"available_only": true}} |
      | get_book     | {"book_id": "b01"}                                       |
      | get_holds    | {"member": "sam"}                                        |
      | place_hold   | {"member": "sam", "book_id": "b01", "request_id": "k1"}  |
