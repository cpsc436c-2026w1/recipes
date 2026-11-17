Feature: Extension: an exact answer cache, looked up before retrieval
  answer_with_cache in ext-answer-cache/answer_cache.py, the function a team copies, and cache_run.py.
  ext-answer-cache/README.md, "Requirements": R50 to R59. Against moto, with a clock the scenario moves and a
  retriever and a model that count their calls.

  Background:
    Given the answer cache table exists
    And a retriever with the public document "Returns within 30 days." and one order per user (user-ana: order 1042, user-ben: order 2077)

  @R50
  Scenario: A hit skips retrieval and the model
    When "user-ana" asks "What is the return policy?" with index version "idx-1"
    And "user-ben" asks "What is the return policy?" with index version "idx-1"
    Then the answers were "miss, hit"
    And the retriever was called 1 time and the model 1 time
    And the last answer is "Returns within 30 days."

  @R51
  Scenario Outline: Questions that differ only in capitals, spaces and the last mark are the same question
    When "user-ana" asks "What is the return policy?" with index version "idx-1"
    And "user-ana" asks "<other>" with index version "idx-1"
    Then the answers were "miss, <second>"

    Examples:
      | other                         | second |
      | what is the   RETURN policy   | hit    |
      | What is the return policy!    | hit    |
      | What is the return policy now | miss   |
      | What is a return policy?      | miss   |

  @R52
  Scenario: A new index version misses
    When "user-ana" asks "What is the return policy?" with index version "idx-1"
    And "user-ana" asks "What is the return policy?" with index version "idx-2"
    Then the answers were "miss, miss"
    And the retriever was called 2 times and the model 2 times

  @R53
  Scenario Outline: An answer that depends on who asks is stored only for the user who asked
    When "user-ana" asks "<question>" with index version "idx-1"
    And "user-ben" asks "<question>" with index version "idx-1"
    And "user-ana" asks "<question>" with index version "idx-1"
    Then the answers were "miss, miss, hit"
    And no answer to "user-ben" holds "1042"

    Examples: a personal word
      | question           |
      | Where is my order? |
    Examples: no personal word, and the retriever returns the asking user's own document
      | question           |
      | Order status       |

  @R54
  Scenario: A question with a personal word is not looked up under the shared key
    Given an answer to "Where is my order?" is stored under the shared key for "idx-1"
    When "user-ben" asks "Where is my order?" with index version "idx-1"
    Then the answers were "miss"

  @R55
  Scenario Outline: A shared entry is a miss once it is 24 hours old, while DynamoDB still stores it
    When "user-ana" asks "What is the return policy?" with index version "idx-1"
    And <seconds> seconds pass
    And "user-ana" asks "What is the return policy?" with index version "idx-1"
    Then the answers were "miss, <second>"
    And the cache table still stores 1 entry

    Examples:
      | seconds | second |
      | 86399   | hit    |
      | 86400   | miss   |

  @R56
  Scenario: The user id is never stored in the cache table
    When "user-ana" asks "Where is my order?" with index version "idx-1"
    Then no item of the cache table holds "user-ana"

  @R58
  Scenario Outline: An entry for one user is a miss once it is 1 hour old
    When "user-ana" asks "<question>" with index version "idx-1"
    And <seconds> seconds pass
    And "user-ana" asks "<question>" with index version "idx-1"
    Then the answers were "miss, <second>"

    Examples: a personal word
      | question           | seconds | second |
      | Where is my order? | 3599    | hit    |
      | Where is my order? | 3600    | miss   |
    Examples: no personal word, and the retriever returns the asking user's own document
      | question           | seconds | second |
      | Order status       | 3599    | hit    |
      | Order status       | 3600    | miss   |

  @R59
  Scenario: Every lookup is counted as a hit or a miss
    When "user-ana" asks "What is the return policy?" with index version "idx-1"
    And "user-ben" asks "What is the return policy?" with index version "idx-1"
    And "user-ben" asks "Where is my order?" with index version "idx-1"
    Then the cache counted 1 hit and 2 misses
