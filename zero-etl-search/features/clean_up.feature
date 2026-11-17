Feature: Clean-up removes what the runs made, for this PREFIX
  README, "Requirements": R50 to R56. clean_up.py runs against moto and the stand-ins.

  Background:
    Given AWS is a stand-in

  @R50
  Scenario: After the set-up and ext-embeddings, clean-up leaves nothing of this PREFIX
    Given main/run.py has run with "--target aws --setup"
    And ext-embeddings has been set up
    And a teammate's table "recipe-bo-items" and queue "recipe-bo-search-indexer-failed"
    When clean_up.py runs
    Then the exit code is 0
    And nothing of "recipe-ana" is left: no table, mapping, function, log group, queue, collection, policy, group or tagged role
    And the teammate's table and queue are still there

  @R51
  Scenario: A dry run deletes nothing
    Given main/run.py has run with "--target aws --setup"
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And no delete call was made

  @R52
  Scenario: A second clean-up finds everything absent and exits 0
    Given main/run.py has run with "--target aws --setup"
    And clean_up.py has run
    When clean_up.py runs
    Then the exit code is 0
    And it printed no "removed" line and no "FAILED" line

  @R53
  Scenario: A refused delete is a FAILED step; the others still run, and the exit code is 1
    Given main/run.py has run with "--target aws --setup"
    And Lambda refuses DeleteFunction with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And it printed a "FAILED" line for "Lambda function recipe-ana-search-indexer" with "AccessDeniedException"
    And the table, the queue and the collection are gone

  @R54
  Scenario Outline: OpenSearch Ingestion denied by a service control policy is "not checked", not a failure
    Given main/run.py has run with "--target aws --setup"
    And OpenSearch Ingestion refuses GetPipeline with "AccessDeniedException" and the message "<message>"
    When clean_up.py runs
    Then the exit code is <code>

    Examples:
      | message                                                        | code |
      | with an explicit deny in a service control policy             | 0    |
      | with an explicit deny in an identity-based policy             | 1    |

  @R55
  Scenario: The mapping of a function that is already gone is still removed
    Given main/run.py has run with "--target aws --setup"
    And the function "recipe-ana-search-indexer" was deleted and its mapping left
    When clean_up.py runs
    Then the exit code is 0
    And no event source mapping is left

  @R56
  Scenario: A role of the recipe's name without the recipe's tag is left
    Given a role "recipe-ana-search-indexer-role" without the tag recipe=zero-etl-search
    When clean_up.py runs
    Then the role "recipe-ana-search-indexer-role" is still there
