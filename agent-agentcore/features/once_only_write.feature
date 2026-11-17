Feature: A write happens once per request key

  @R30
  Scenario: The write carries a key computed from the request id, the member and the book
    Given the request id is "req-1"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the store has one row, with the request key of "req-1", "sam" and "b02"

  @R31
  Scenario: The same write asked twice in one request adds one row
    Given the request id is "req-1"
    And the stub model behaves "asks_twice"
    When member "sam" asks "Which is shorter, Dune or Emma? Place a hold on the shorter one for me." and answers "y"
    Then the tools that ran are "search_books, search_books, place_hold, place_hold"
    And the store has one row, with the request key of "req-1", "sam" and "b02"

  @R31
  Scenario: A second write with a key the store holds returns the stored row
    When the tool "place_hold" is called with '{"member": "sam", "book_id": "b02", "request_id": "key-1"}'
    Then the result says placed "true" and duplicate_request "false"
    When the tool "place_hold" is called with '{"member": "sam", "book_id": "b02", "request_id": "key-1"}'
    Then the result says placed "false" and duplicate_request "true"
    And member "sam" has holds on "b02"

  @R32
  Scenario: A write without a request key is refused
    When the tool "place_hold" is called with '{"member": "sam", "book_id": "b02"}'
    Then the result is an error that says "needs a request_id"
    And member "sam" has no holds
