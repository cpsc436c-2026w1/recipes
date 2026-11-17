Feature: Set-up in AWS
  README, "Requirements": R30 to R39. main/run.py runs with "--target aws --setup" (create or update everything,
  write nothing to the table) against moto and the stand-ins of features/support.py.

  Background:
    Given AWS is a stand-in

  @R30 @R33 @R34 @R61
  Scenario: The table, the failure queue and the log group
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 0
    And the table "recipe-ana-items" is billed per request, keyed by "id", with a stream of new and old images
    And the queue "recipe-ana-search-indexer-failed" keeps messages 1209600 seconds
    And the log group "/aws/lambda/recipe-ana-search-indexer" keeps logs 1 day
    And every AWS call went to ca-central-1

  @R31
  Scenario: The function
    When main/run.py runs with "--target aws --setup"
    Then the function "recipe-ana-search-indexer" runs python3.13 on arm64 with 128 MB and a 60 second time-out
    And its zip holds handler.py only
    And its environment names the collection's endpoint, the index "items-v1" and index version "1"

  @R32
  Scenario: The event source mapping
    When main/run.py runs with "--target aws --setup"
    Then the mapping from the table's stream to "recipe-ana-search-indexer" has batch size 10, batching window 0, bisect on, ReportBatchItemFailures, 10 retries, maximum record age 3600 and starting position TRIM_HORIZON
    And its on-failure destination is the queue "recipe-ana-search-indexer-failed"

  @R35
  Scenario: The collection group and the collection
    When main/run.py runs with "--target aws --setup"
    Then the collection group "recipe-ana-search" is NEXTGEN with standby replicas ENABLED and 0 to 2 OCU for indexing and for search
    And the collection "recipe-ana-items" is a SEARCH collection in that group
    And the index "items-v1" was created with dynamic false and index_version 1 in its _meta

  @R36
  Scenario: Who may do what, with no wildcard resource
    When main/run.py runs with "--target aws --setup"
    Then the data access policy of "recipe-ana-items" lets the function's role write documents in "items-v1" only
    And it lets the identity that ran the script create, describe, read and delete "items-v1" and describe the collection
    And no resource in the data access policy or in the function role's policy has a "*" outside a path
    And the function role may call the data API of the collection "recipe-ana-items" only

  @R37
  Scenario: The mapping is created only after the function could write
    Given the function's probe answers "cannot write" twice
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 0
    And the probe was invoked 3 times, every 10 seconds, before the mapping was created

  @R38
  Scenario Outline: A new collection that refuses the scripts at first is tried again every 10 s
    Given the collection's endpoint answers <status> to the first 4 requests
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 0
    And it printed "collection let you in"
    And the index create was refused and tried again after 10 seconds, 2 times

    Examples:
      | status |
      | 401    |
      | 403    |

  @R38
  Scenario: A collection that refuses for 300 s stops the set-up
    Given the collection's endpoint answers 401 to the first 60 requests
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 1

  @R39
  Scenario: A queue name deleted less than a minute ago is tried again every 10 s
    Given SQS refuses CreateQueue with "AWS.SimpleQueueService.QueueDeletedRecently" twice
    When main/run.py runs with "--target aws --setup"
    Then the exit code is 0
    And CreateQueue was called 3 times, 10 seconds apart

  @R44 @R36
  Scenario: A rerun by a teammate adds their role and keeps yours
    Given main/run.py has run with "--target aws --setup"
    And a teammate runs the scripts as "arn:aws:iam::123456789012:role/teammate"
    When main/run.py runs with "--target aws --setup"
    Then the data access policy names the first identity, the teammate and the function's role
    And the teammate may create, search and delete the index and may not write documents
