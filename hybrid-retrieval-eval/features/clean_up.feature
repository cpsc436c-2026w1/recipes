Feature: Clean-up removes what a run made, for this PREFIX
  README, "Requirements": R50 to R55. clean_up.py runs against the OpenSearch Serverless stand-in.

  Background:
    Given OpenSearch Serverless is a stand-in

  @R50
  Scenario: Clean-up after a run removes the five resources of this PREFIX and nothing else
    Given everything of an earlier run is still there
    And a teammate's collection "recipe-bo-products" with its group and policies
    When clean_up.py runs
    Then the exit code is 0
    And it printed "removed" for the collection, the group, the data access, network and encryption policies of "recipe-ana-products"
    And nothing named "recipe-ana-products" is left
    And the teammate's collection, group and policies are still there

  @R51
  Scenario: A dry run lists the same five and deletes nothing
    Given everything of an earlier run is still there
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And it printed "would remove" 5 times
    And no delete call was made

  @R52
  Scenario: A second clean-up finds everything absent and exits 0
    When clean_up.py runs
    Then the exit code is 0
    And it printed "absent" 5 times

  @R53
  Scenario: A refused delete is a FAILED step; the other steps still run, and the exit code is 1
    Given everything of an earlier run is still there
    And OpenSearch Serverless refuses DeleteCollection with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And it printed "FAILED" for the collection with "AccessDeniedException"
    And it printed "FAILED" for the group, which cannot go while its collection is there
    And it printed "removed" for the data access, network and encryption policies of "recipe-ana-products"

  @R54
  Scenario: A collection that an earlier clean-up is still deleting is absent, and waited out
    Given everything of an earlier run is still there
    And the collection "recipe-ana-products" is being deleted
    When clean_up.py runs
    Then the exit code is 0
    And it printed "absent" for "collection recipe-ana-products (already being deleted)"
    And the group was deleted after the collection was gone

  @R55
  Scenario: Clean-up waits until the group is gone before it ends
    Given everything of an earlier run is still there
    When clean_up.py runs
    Then the last call about the group found no group
