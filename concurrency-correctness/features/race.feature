Feature: Two clients accept the same trip
  main/run.py, with moto in place of DynamoDB. README, "Requirements": R10 to R16, R20 to R27, R30 to R32.

  @R10
  Scenario: Without a condition both drivers are told yes
    When main/run.py runs with "--target local --rounds 6"
    Then the exit code is 0
    And in "unsafe" mode 6 of 6 rounds told both drivers yes and 0 rounds had one winner

  @R11 @R13
  Scenario: With the condition one driver wins and is the one the table holds
    When main/run.py runs with "--target local --rounds 6"
    Then in "safe" mode 6 of 6 rounds had one winner, 0 rounds told both drivers yes, and the winner is stored in 6
    And every trip of the "safe" rounds holds one driver and version 2

  @R12
  Scenario: The refused driver is told who holds the trip
    When main/run.py runs with "--target local --rounds 1"
    Then the printed race on trip "t-0042" has one driver "accepted" and one "refused, the trip is held by" the other
    And the table line of that race names the accepted driver and version 2
    And every conditional write asked for the item that blocks it
    And each round read its trip 3 times: once per client and once at the end

  @R16
  Scenario: A driver's accept is sent again after its reply was lost
    Given a trip "t-0007" that waits, read by driver "d-7" at version 1
    When driver "d-7" sends the safe accept for "t-0007" with version 1
    And driver "d-7" sends the safe accept for "t-0007" with version 1
    Then both answers say accepted
    And trip "t-0007" holds driver "d-7" at version 2
    And 1 of the 2 writes was refused by DynamoDB

  @R16
  Scenario: Another driver's accept after that is still refused
    Given a trip "t-0007" that waits, read by driver "d-7" at version 1
    When driver "d-7" sends the safe accept for "t-0007" with version 1
    And driver "d-9" sends the safe accept for "t-0007" with version 1
    Then the last answer says refused, held by "d-7"
    And trip "t-0007" holds driver "d-7" at version 2

  @R14
  Scenario: Every read before a decision is a strong read
    When main/run.py runs with "--target local --rounds 3"
    Then every GetItem call was a strongly consistent read

  @R15
  Scenario: Both clients read before either writes, in every round
    When main/run.py runs with "--target local --rounds 8"
    Then on every trip the first two reads come before the first accept

  @R20
  Scenario: Twenty rounds in each mode unless --rounds says otherwise
    When main/run.py runs with "--target local"
    Then in "unsafe" mode 20 of 20 rounds told both drivers yes and 0 rounds had one winner
    And in "safe" mode 20 of 20 rounds had one winner, 0 rounds told both drivers yes, and the winner is stored in 20
    And each mode ran on 20 trips of its own turn, none used twice in a mode

  @R21
  Scenario: The one race the run writes out
    When main/run.py runs with "--target local --rounds 1"
    Then the output holds "one race on trip t-0042, safe mode:"
    And under it one line says a driver was "accepted", one that the other was "refused, the trip is held by" the first, and one what the table holds
    And the output holds "target: local, table: recipe-ana-race-trips"

  @R22
  Scenario: A run on AWS removes its table at the end
    When main/run.py runs with "--target aws --rounds 2"
    Then the exit code is 0
    And the account has no tables
    And the output holds "removed  table recipe-ana-race-trips"

  @R23
  Scenario: --keep leaves the table
    When main/run.py runs with "--target aws --rounds 2 --keep"
    Then the exit code is 0
    And the account has exactly the tables "recipe-ana-race-trips"
    And no DeleteTable call was made

  @R24
  Scenario: The clean-up at the end of a run is refused
    Given AWS refuses every "DeleteTable" call with "AccessDeniedException"
    When main/run.py runs with "--target aws --rounds 2"
    Then the exit code is 1
    And the output holds "FAILED   table recipe-ana-race-trips: AccessDeniedException"
    And the output holds "the clean-up is NOT complete"
    And the output does not hold "target: aws"

  @R25
  Scenario: The table of an earlier run is still being deleted when a run starts
    Given the table "recipe-ana-race-trips" is in the DELETING state until the recipe has waited for it
    When main/run.py runs with "--target aws --rounds 1 --keep"
    Then the exit code is 0
    And the recipe asked for the table 2 times, and looked whether it was gone in between
    And the account has exactly the tables "recipe-ana-race-trips"

  @R26
  Scenario: The table of an earlier run is there
    Given main/run.py ran with "--target aws --rounds 1 --keep"
    When main/run.py runs with "--target aws --rounds 2 --keep"
    Then the exit code is 0
    And in "safe" mode 2 of 2 rounds had one winner, 0 rounds told both drivers yes, and the winner is stored in 2
    And no DeleteTable call was made

  @R27
  Scenario: The local run needs no account
    Given no AWS credentials and no Region in the environment
    When main/run.py runs with "--target local --rounds 2"
    Then the exit code is 0
    And in "safe" mode 2 of 2 rounds had one winner, 0 rounds told both drivers yes, and the winner is stored in 2

  @R30
  Scenario Outline: Every table name starts with PREFIX
    When with PREFIX "<prefix>", main/run.py runs with the arguments "--target aws --rounds 1 --keep"
    Then every call that names a table names "<table>"

    Examples:
      | prefix     | table                 |
      | recipe-bo  | recipe-bo-race-trips  |
      | not set    | recipe-race-trips     |

  @R32
  Scenario Outline: Each table has a ceiling on its throughput
    When <script> runs with "--target aws --keep"
    Then <n> CreateTable calls were made, each with a maximum of 100 read and 100 write request units per second

    Examples:
      | script                       | n |
      | main/run.py                  | 1 |
      | ext-idempotent-retry/run.py  | 2 |

  @R31
  Scenario Outline: The Region of every call
    Given AWS_REGION is "<set>"
    When main/run.py runs with "--target aws --rounds 1"
    Then every call went to the Region "<used>"

    Examples:
      | set       | used         |
      | not set   | ca-central-1 |
      | ca-west-1 | ca-west-1    |
