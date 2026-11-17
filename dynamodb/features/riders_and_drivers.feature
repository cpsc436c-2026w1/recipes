@moto
Feature: Riders and drivers: a second key, transactions, a version number
  ext-riders-and-drivers/run.sh after main/run.sh, both for real against a local stand-in.
  ext-riders-and-drivers/README.md, "Requirements": R50 to R55.

  Background:
    Given main/run.sh has run against the local stand-in with KEEP=1 and PREFIX "recipe-ana"
    And ext-riders-and-drivers/run.sh has run after it with KEEP=1

  @R50
  Scenario: A rider's history for a range of days
    Then step "4" returned the trips of rider "r-03" from "2026-10-03" to "2026-10-06T23:59:59Z" that the table "recipe-ana-rider-trips" holds
    And the trips of step "4" are in time order
    And step "4" says the Query read as many items as it returned

  @R51
  Scenario: A rider's latest trip
    Then step "5" returned the newest of the loaded trips of rider "r-03"

  @R52
  Scenario: A new trip is in both tables
    Then step "6" made 1 transaction with a Put in "recipe-ana-trips" and a Put in "recipe-ana-rider-trips"
    And trip "t-0100" is in the table "recipe-ana-trips"
    And trip "t-0100" is in the table "recipe-ana-rider-trips"

  @R53
  Scenario: A busy driver accepts a second trip
    Then the error output holds "TransactionCanceledException"
    And trip "t-0101" has no driver and status "requested"
    And driver "d-3" has trip "t-0100" and status "busy"

  @R54
  Scenario: A fare write with an old version
    Then the output of step "10" holds "refused: the version moved on"
    And trip "t-0100" has fare "24" and version "2"

  @R55
  Scenario: A second run of the extension
    Given the items of the three tables are noted
    When "ext-riders-and-drivers/run.sh" runs against the local stand-in with KEEP=1 and PREFIX "recipe-ana"
    Then the exit code is 0
    And the three tables hold the items that were noted
