Feature: Workflow patterns on the stub
  App code decides every next step: the order of a chain, which calls run side by side, how replies are combined,
  and when a loop ends. README, "Requirements": R10 to R40.

  @R10
  Scenario: The chain makes its three calls in a fixed order
    When the chain runs on its stub
    Then the model calls are "rewrite, pick, answer"
    And call 2 has the reply of call 1 as its user message
    And call 3 has the reply of call 1 in its user message
    And the result has gate "passed"

  @R11
  Scenario: A pick with a product the shop does not sell stops the chain
    When the chain runs with the pick "oat milk, soy milk"
    Then the model calls are "rewrite, pick"
    And the result has gate "stopped"
    And the result has reply "None"

  @R20
  Scenario: Sectioning has both calls in flight at the same time
    When sectioning runs on a stub that answers only when 2 calls are in flight
    Then 2 model calls were made and all of them overlap
    And the result has shown "Sorry about the late, open carton; yes, we have rye bread."

  @R21
  Scenario: A message the screen call does not pass gets the stock reply
    When sectioning runs with the screen reply "off_topic"
    Then the result has shown "I can only help with groceries and orders."

  @R22
  Scenario: Voting sends the same task three times at once
    When voting runs on a stub that answers only when 3 calls are in flight
    Then 3 model calls were made and all of them overlap
    And every model call has the same system prompt and user message

  @R23
  Scenario: The label of the majority wins
    When voting runs with the votes "complaint, question, complaint"
    Then the result has label "complaint"
    And the result has agreed "2 of 3"

  @R24
  Scenario: Three different votes are no majority
    When voting runs with the votes "complaint, question, order"
    Then the result has label "no_majority"

  @R30
  Scenario Outline: Feedback on a failed draft reaches the next generator call
    When the evaluator loop runs on its stub with <evaluator> as the evaluator
    Then the model calls are "<calls>"
    And the second generator call has "at most 12 allowed" in its user message
    And the result has passed "True"
    And the result has rounds "2"

    Examples:
      | evaluator      | calls                                    |
      | a model call   | generator, evaluator, generator, evaluator |

    @R31
    Examples:
      | evaluator      | calls                |
      | the code check | generator, generator |

  @R32
  Scenario Outline: The loop stops at the round limit
    When the evaluator loop runs with <evaluator> as the evaluator and drafts that never pass
    Then <calls> model calls were made
    And the result has passed "False"
    And the result has rounds "3"

    Examples:
      | evaluator      | calls |
      | a model call   | 6     |
      | the code check | 3     |

  @R40
  Scenario: The call over the cap is not made
    When the chain runs on its stub with a cap of 2 model calls
    Then the run stopped with "call cap reached: 2 model calls"
    And 2 model calls were made
