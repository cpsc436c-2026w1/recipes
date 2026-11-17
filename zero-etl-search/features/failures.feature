Feature: A refused record is set aside; temporary errors are retried
  README, "Requirements": R20 to R26.

  Background:
    Given the function's index is the recipe's stand-in index "items-v1"

  @R20 @R22
  Scenario: A refused record that is not first: the function reports it, and does not send it again
    When the function gets one batch: INSERT "I01" named "A", INSERT "I02" priced "free", INSERT "I03" named "C"
    Then the function sent 1 request
    And the function returned the sequence number of "I02" as its batch item failure
    And the index holds 2 documents

  @R21
  Scenario: A refused record that is first: the function fails, so Lambda splits the batch
    When the function gets one batch: INSERT "I02" priced "free", INSERT "I01" named "A"
    Then the function failed with "1 documents refused"

  @R23
  Scenario Outline: A temporary error is sent again, after a wait with jitter
    Given the index first answers <first>
    When the function gets one batch: INSERT "I01" named "A"
    Then the function sent 2 requests
    And the function waited once, between 0.25 and 0.5 seconds
    And the index holds 1 document

    Examples:
      | first   |
      | 403     |
      | 408     |
      | 429     |
      | 500     |
      | 502     |
      | 503     |
      | 504     |
      | timeout |

  @R23
  Scenario: The waits grow 0.5, 1, 2, 4, 8 s, then stay at 8 s; at most 8 calls
    Given the index answers 503 to the first 10 requests
    And the function's random part of each wait is the largest
    When the function gets one batch: INSERT "I01" named "A"
    Then the function sent 8 requests
    And the function waited 0.5, 1, 2, 4, 8, 8, 8 seconds
    And the function returned the sequence number of "I01" as its batch item failure

  @R24
  Scenario Outline: Retries stop when the next wait would leave less than 5 seconds
    Given the index answers 503 to the first 10 requests
    And the function's random part of each wait is the largest
    And the function has <left> seconds left
    When the function gets one batch: INSERT "I01" named "A"
    Then the function sent <n> requests
    And the function returned the sequence number of "I01" as its batch item failure

    Examples:
      | left | n |
      | 9    | 4 |
      | 5.5  | 2 |
      | 5    | 1 |

  @R25
  Scenario: One log line per batch
    When the function gets one batch: INSERT "I01" named "A", REMOVE "I09"
    Then the function logged one line with records 2, indexed 1, deleted 1, attempts 1

  @R26
  Scenario: Through the stand-in mapping, the refused record ends alone in the failure queue, and the next one is indexed
    When main/run.py runs with no options
    Then it printed "bad record I11 (price_cad 'free'): in the index False; failure queue: 1 message(s), batch size [1], item ['I11']"
    And it printed "INSERT I12 written right after it: searchable on the next delivery"
