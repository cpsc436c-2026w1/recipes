Feature: Caps on model calls, tool calls and time are kept by a hook
  The request in every scenario: a member asks which of two books is shorter and wants a hold on it.

  @R10
  Scenario: The counts of one request are in its report
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the status is "done"
    And the report counts 4 model calls and 3 tool calls
    And the model was called 4 times

  @R11
  Scenario: A tool call past the cap is refused and does not run
    Given the setting "MAX_TOOL_CALLS" is "2"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the tools that ran are "search_books, search_books"
    And a tool call was refused with "cap of 2 tool calls"
    And member "sam" has no holds

  @R12
  Scenario: A model that never stops is ended at the cap on model calls
    Given the stub model behaves "runaway"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the status is "stopped"
    And the run was stopped by "cap of 8 model calls"
    And the model was called 8 times

  @R12
  Scenario: The command ends with exit code 2 when a cap stopped the request
    When the command "ask --model stub --stub runaway --yes" runs
    Then the exit code is 2
    And the output has "stopped by the app: cap of 8 model calls"

  @R14
  Scenario: A model call that the gateway refuses ends the request and is not sent again
    Given a local stand-in for the course LLM gateway
    And the stand-in refuses every model call with status 429
    When the command "ask --model gateway --yes" runs
    Then the exit code is 1
    And the output has "error after 1 model calls"
    And the stand-in got 1 request

  @R13
  Scenario: A request past its deadline makes no further model call
    Given the request has already run longer than DEADLINE_S
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the status is "stopped"
    And the run was stopped by "deadline of 120 s"
    And the model was called 0 times
