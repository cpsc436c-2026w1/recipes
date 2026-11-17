Feature: The index follows the table
  README, "Requirements": R10 to R16. The function is called at its entry point, main/handler.py handler(), with
  records in the shape Lambda passes them; its requests reach the recipe's stand-in index (main/standin.py).

  Background:
    Given the function's index is the recipe's stand-in index "items-v1"

  @R10 @R12
  Scenario: An insert and an update are indexed under the item's key
    When the function gets an INSERT of item "I01" named "Rain shell" priced 120.0
    And the function gets a MODIFY of item "I01" named "Rain shell 2" priced 110.0
    Then the index holds 1 document, "I01", named "Rain shell 2" priced 110.0
    And the document of "I01" has index_version 1, changed_at and indexed_at, and no "id" field
    And every request went to "/_bulk" of the collection's endpoint, signed for the service "aoss"

  @R11
  Scenario: A delete removes the document
    Given the index holds item "I01"
    When the function gets a REMOVE of item "I01"
    Then the index holds 0 documents

  @R13
  Scenario: Within one batch only the last change of an item is sent
    When the function gets one batch: INSERT "I01" named "A", MODIFY "I01" named "B", MODIFY "I01" named "C"
    Then the function sent 1 request with 1 action
    And the index holds 1 document, "I01", named "C" priced 10.0

  @R14
  Scenario: The same batch twice leaves the same documents
    When the function gets one batch: INSERT "I01" named "A", INSERT "I02" named "B"
    And the function gets that batch again
    Then the index holds 2 documents
    And the function returned no batch item failure both times

  @R15
  Scenario: A delete of a document that is not there is done, not a failure
    When the function gets a REMOVE of item "I77"
    Then the function returned no batch item failure

  @R16
  Scenario: The offline main scenario
    Given PREFIX is "recipe"
    When main/run.py runs with no options
    Then the exit code is 0
    And the printed lines are those of "output/main-local.txt", scores aside
