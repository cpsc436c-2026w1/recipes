@moto
Feature: Clean-up against the local stand-in
  The real AWS CLI and clean_up.sh against moto. README, "Requirements": R40, R48.

  @fresh @R40 @R48
  Scenario: A teammate's tables stay
    Given main/run.sh has run against the local stand-in with KEEP=1 and PREFIX "recipe-ana"
    And ext-riders-and-drivers/run.sh has run after it with KEEP=1
    And the tables "recipe-bo-trips, recipe-ana-trips-archive, trips" of other owners exist
    When "clean_up.sh" runs against the local stand-in with PREFIX "recipe-ana"
    Then the exit code is 0
    And the local stand-in has exactly the tables "recipe-ana-trips-archive, recipe-bo-trips, trips"
