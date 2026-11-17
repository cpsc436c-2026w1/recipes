Feature: A retried write applies once
  ext-idempotent-retry/run.py and the functions of tips.py, with moto in place of DynamoDB. The first reply of a
  write is lost by the recipe's own ReplyLostOnce. ext-idempotent-retry/README.md, "Requirements": R50 to R58.

  @R50 @R52
  Scenario: One tap, one lost reply, with and without a request id
    When ext-idempotent-retry/run.py runs with "--target local"
    Then the exit code is 0
    And the naive write was sent 2 times and the tip on "t-tip-naive" is 10
    And the transaction was sent 2 times, both with the same request id, and the tip on "t-tip-once" is 5
    And the output holds "tip after one tap and one retry: naive 10, with request id 5"

  @R51
  Scenario: The second arrival of a request id changes nothing
    When ext-idempotent-retry/run.py runs with "--target local"
    Then the output holds "attempt 2: already applied"
    And the requests table holds 1 marker, for trip "t-tip-once" and amount 5

  @R53
  Scenario: A tip for a trip that does not exist
    Given the two tables of PREFIX "recipe-ana" exist
    When add_tip_once is called for trip "t-nope" with a new request id
    Then the call raises "TransactionCanceledException"
    And trip "t-nope" does not exist
    And the requests table holds 0 markers

  @R54
  Scenario: No reply after three attempts
    Given no "UpdateItem" call gets a reply
    And the recipe's waits are written down, not waited
    When ext-idempotent-retry/run.py runs with "--target local"
    Then the run stops with the error "no reply after 3 attempts"
    And the naive write was sent 3 times
    And the output holds "attempt 3: timed out (ReadTimeoutError), giving up"

  @R58
  Scenario: The waits between attempts are random and bounded, and there is none after the last
    Given no "UpdateItem" call gets a reply
    And the recipe's waits are written down, not waited
    When ext-idempotent-retry/run.py runs with "--target local", 12 times
    Then each run waited 2 times: at most 0.2 seconds, then at most 0.4 seconds
    And the 12 first waits are not all the same

  @R55
  Scenario: A marker expires after seven days
    When ext-idempotent-retry/run.py runs with "--target aws --keep"
    Then the marker's "expires_at" is 7 days after now, within a minute
    And time to live is on for "expires_at" in the table "recipe-ana-race-requests"

  @R56
  Scenario: The transaction carries the request id as DynamoDB's own token
    When ext-idempotent-retry/run.py runs with "--target local"
    Then every TransactWriteItems call has its marker's request id as ClientRequestToken

  @R57
  Scenario: A run on AWS removes both tables at the end
    When ext-idempotent-retry/run.py runs with "--target aws"
    Then the exit code is 0
    And the account has no tables

  @R57
  Scenario: A second run over kept tables
    Given ext-idempotent-retry/run.py ran with "--target aws --keep"
    And AWS refuses every "UpdateTimeToLive" call with "ValidationException"
    When ext-idempotent-retry/run.py runs with "--target aws --keep"
    Then the exit code is 0
    And the account has exactly the tables "recipe-ana-race-requests, recipe-ana-race-trips"
