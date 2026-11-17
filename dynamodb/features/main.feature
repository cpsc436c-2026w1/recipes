@moto
Feature: The trip store, the main scenario
  main/run.sh creates the trips table, loads 50 trips, reads one trip the eventual and the strong way, lets two
  drivers race for it, and asks a question no key answers.
  README, "Requirements": R10 to R18, R30, R33. The script runs for real, with the AWS CLI against a local stand-in.

  Background:
    Given main/run.sh has run against the local stand-in with KEEP=1 and PREFIX "recipe-ana"

  @R10 @R30
  Scenario: The table and its key
    Then step "1" created the table "recipe-ana-trips" with partition key "trip_id" and billing mode "PAY_PER_REQUEST"
    And the table "recipe-ana-trips" has the index "waiting-by-zone" with partition key "pickup_zone" and sort key "status"
    And the local stand-in has exactly the tables "recipe-ana-trips"

  @R33
  Scenario: The tags of the table
    Then the table "recipe-ana-trips" has the tags "purpose=recipe, prefix=recipe-ana"

  @R11
  Scenario: The load
    Then step "2" made 2 batch-write-item calls, each with 25 items
    And the table "recipe-ana-trips" holds 50 trips

  @R12
  Scenario: Two drivers accept one trip
    Then trip "t-0042" has driver "d-7" and status "accepted"

  @R13
  Scenario: The second driver is refused and told who has the trip
    Then the error output holds "ConditionalCheckFailedException"
    And the error output shows the blocking item with driver "d-7"
    And the output of step "6" holds "refused: d-7 keeps the trip"

  @R14
  Scenario: The read after the accept is strong
    Then the get-item command of step "7" has "--consistent-read"
    And step "7" printed driver "d-7" and status "accepted"

  @R15
  Scenario: A question that names no key reads every item
    Then step "8" says the Scan read as many items as the table "recipe-ana-trips" holds
    And step "8" says the Scan returned as many items as wait at "JFK" in that table

  @R18
  Scenario: The same question through the index reads only what it returns
    Then the command of step "9" is a Query on the index "waiting-by-zone"
    And step "9" says the Query read as many items as it returned
    And step "9" says the Query returned as many items as wait at "JFK" in that table

  @R16
  Scenario: Every billed call prints its units
    Then every command of steps "2, 3, 4, 5, 6, 8, 9" asks for the consumed units
    And the answers of steps "2, 5" name "write_units"
    And the answers of steps "3, 4, 8, 9" name "read_units"
    And step "6", the refused accept, printed no units and says that a refused write reports none
