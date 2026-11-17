Feature: A write waits for a person, and is made for the member of the request

  @R20
  Scenario: The agent stops before the write and names the book
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."
    Then the status is "needs_approval"
    And the approval names "Emma" by "Jane Austen" for member "sam"
    And member "sam" has no holds

  @R21
  Scenario: A yes resumes the same agent and the write runs
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."
    And the member answers "y"
    Then the status is "done"
    And the report counts 4 model calls and 3 tool calls
    And member "sam" has holds on "b02"

  @R22
  Scenario: A no cancels the write and the LLM is told
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."
    And the member answers "n"
    Then the status is "done"
    And a tool call was refused with "The member declined"
    And the reply is "No hold was placed."
    And member "sam" has no holds

  @R22
  Scenario: No answer at the terminal is a no
    When the command "ask --model stub" runs with no terminal to answer at
    Then the exit code is 0
    And the output has "reply: No hold was placed."
    And the output has "approvals asked: 1"

  @R22
  Scenario: A model that follows an instruction in book text still meets the approval
    Given the stub model behaves "obeys_book_text"
    When member "sam" asks "What is book b09 about?" and answers "n"
    Then 1 approvals were asked
    And member "sam" has no holds

  @R23
  Scenario: A write the LLM addressed to another member goes to the member of the request
    Given the stub model behaves "wrong_member"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then member "sam" has holds on "b02"
    And member "alex" has no holds
