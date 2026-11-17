Feature: Moving to a new index version, and taking a principal out
  README, "Requirements": R40 to R46. main/run.py --setup and main/migrate.py against the stand-ins.

  Background:
    Given AWS is a stand-in
    And main/run.py has run with "--target aws --setup"

  @R40
  Scenario: Set-up with INDEX_VERSION=2 builds items-v2 beside items-v1 and moves the function
    Given INDEX_VERSION is "2"
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 0
    And the endpoint has the indexes "items-v1" and "items-v2"
    And the data access policy names "items-v1" and "items-v2" for the function's role
    And the function's environment names the index "items-v2"
    And no item was written to the table

  @R41
  Scenario: The backfill touches every item that still exists, through the table
    Given the table holds the items "I01", "I02" and "I03"
    When main/migrate.py runs with "backfill"
    Then every UpdateItem of the backfill sets index_backfill on its item with the condition attribute_exists(id)
    And it printed "{'items': 3, 'gone meanwhile': 0}"

  @R42
  Scenario: The index the function writes to cannot be dropped
    When main/migrate.py runs with "drop items-v1"
    Then the exit code is 1
    And it printed "items-v1 is the index the function recipe-ana-search-indexer writes to now"
    And the endpoint has the indexes "items-v1"

  @R43
  Scenario: After the move, drop deletes the old index and takes it out of the policy
    Given INDEX_VERSION is "2"
    And main/run.py has run with "--target aws --setup"
    When main/migrate.py runs with "drop items-v1"
    Then the exit code is 0
    And the endpoint has the indexes "items-v2"
    And no statement of the data access policy names "items-v1"

  @R45
  Scenario Outline: drop-principal takes out a teammate, and refuses you and the function's role
    Given a teammate runs the scripts as "arn:aws:iam::123456789012:role/teammate"
    And main/run.py has run with "--target aws --setup"
    And I run the scripts as myself again
    When main/migrate.py runs with drop-principal for <who>
    Then the exit code is <code>
    And the data access policy <still> names <who>

    Examples:
      | who                 | code | still     |
      | the teammate        | 0    | no longer |
      | myself              | 1    | still     |
      | the function's role | 1    | still     |

  @R46
  Scenario Outline: check compares the table and the index
    Given the table holds the items "I01", "I02" and "I03"
    And the index "items-v1" holds <docs> documents
    And the failure queue holds <queued> message
    When main/migrate.py runs with "check"
    Then the exit code is <code>
    And it printed "table recipe-ana-items: 3 items; index items-v1: <docs> documents; failure queue: <queued> message(s)"

    Examples:
      | docs | queued | code |
      | 3    | 0      | 0    |
      | 2    | 1      | 1    |
      | 2    | 0      | 1    |
      | 4    | 0      | 1    |

  @R46
  Scenario: check waits while the collection refuses at first
    Given the table holds the items "I01", "I02" and "I03"
    And the index "items-v1" holds 3 documents
    And the failure queue holds 0 message
    And the collection's endpoint answers 403 to the first 2 requests
    When main/migrate.py runs with "check"
    Then the exit code is 0
    And it printed "refused (403); trying again in 10 s"
