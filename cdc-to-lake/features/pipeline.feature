Feature: The pipeline in the sandbox
  `python -m main.run --target aws`, called inside moto. Athena's answers are the stand-in's: the view shows the
  changes at the third look. README, "Requirements": R30 to R39, R40 to R42, R57.

  Background:
    Given Athena shows the changes of the scenario at its 3rd look at trips_current

  @R30 @R31 @R42
  Scenario: The table with its stream, and the bucket
    When main.run runs with "--target aws"
    Then the exit code is 0
    And the table "recipe-ana-trips" was created with a stream of new and old images and on-demand capacity
    And the table takes at most 100 read and 100 write request units per second
    And public access is blocked on the bucket "recipe-ana-cdc-lake-123456789012" in all four ways

  @R32
  Scenario: The Iceberg table and the view
    When main.run runs with "--target aws"
    Then one Athena statement creates the Iceberg table "recipe_ana_cdc.trip_changes" at "s3://recipe-ana-cdc-lake-123456789012/lake/trip_changes/", partitioned by "day(change_time)"
    And one Athena statement creates the view "recipe_ana_cdc.trips_current"

  @R33
  Scenario Outline: The Firehose stream
    Given the setting BUFFER_SECONDS is "<set>"
    When main.run runs with "--target aws"
    Then the Firehose stream "recipe-ana-trip-changes" writes to the table "recipe_ana_cdc.trip_changes", append-only
    And the Firehose stream buffers for <seconds> seconds
    And the output holds "buffer <seconds> s"

    Examples:
      | set     | seconds |
      | not set | 60      |
      | 20      | 20      |

  @R34
  Scenario: The function
    When main.run runs with "--target aws"
    Then the function "recipe-ana-cdc-to-firehose" was created with "python3.12", 256 MB, a timeout of 60 seconds and the handler "handler.handler"
    And the function's package holds only "handler.py", the same bytes as main/handler.py
    And the function is told the stream "recipe-ana-trip-changes" in DELIVERY_STREAM

  @R35
  Scenario: How the function reads the stream
    When main.run runs with "--target aws"
    Then the event source mapping starts at "TRIM_HORIZON", takes batches of 100 after at most 5 seconds, reports partial failures, splits a failed batch and gives up on a batch after 10 retries
    And what Lambda gives up on goes to the queue "recipe-ana-cdc-failures", which exists

  @R35
  Scenario: A mapping from an earlier run without the failures queue gets it
    Given the pipeline of a run with --target aws exists
    And its event source mapping has no on-failure destination
    When main.run runs with "--target aws"
    Then the exit code is 0
    And no second event source mapping was created
    And the event source mapping sends what Lambda gives up on to the queue "recipe-ana-cdc-failures"

  @R36
  Scenario: What each role may do
    When main.run runs with "--target aws"
    Then the roles "recipe-ana-cdc-lambda-role, recipe-ana-cdc-firehose-role" were created, each for one service
    And no statement of a role's policy has "*" as its resource or in an action
    And the Lambda role may read only the stream of "recipe-ana-trips" and write only to the Firehose stream "recipe-ana-trip-changes"
    And the Lambda role may send only to the queue "recipe-ana-cdc-failures"
    And the Firehose role may use only the bucket "recipe-ana-cdc-lake-123456789012" and the Glue database "recipe_ana_cdc"

  @R37
  Scenario: Roles that are passed in
    Given LAMBDA_ROLE_ARN and FIREHOSE_ROLE_ARN name the roles "team-lambda" and "team-firehose" of the account
    When main.run runs with "--target aws"
    Then the exit code is 0
    And no role was created
    And the function runs as "arn:aws:iam::123456789012:role/team-lambda"
    And Firehose assumes "arn:aws:iam::123456789012:role/team-firehose"

  @R38
  Scenario: The run waits until the changes are queryable
    When main.run runs with "--target aws"
    Then Athena was asked for trips_current 3 times, 15 seconds apart
    And the output holds "(polled every 15 s)"
    And the printed trips_current has exactly the trips "t-001, t-002"

  @R39
  Scenario: The table of an earlier run is still being deleted
    Given the table "recipe-ana-trips" is in the DELETING state until the recipe has waited for it
    When main.run runs with "--target aws"
    Then the exit code is 0
    And the recipe asked for the table 2 times, and looked whether it was gone in between

  @R39
  Scenario: The Firehose stream of an earlier run is still being deleted
    Given the Firehose stream "recipe-ana-trip-changes" answers DELETING 2 times and is gone after that
    When main.run runs with "--target aws"
    Then the exit code is 0
    And 1 CreateDeliveryStream call was made, after 2 looks that found the stream being deleted

  @R40
  Scenario Outline: Every name starts with PREFIX
    Given the setting PREFIX is "<prefix>"
    When main.run runs with "--target aws"
    Then the account has exactly the table "<p>-trips", the function "<p>-cdc-to-firehose", the Firehose stream "<p>-trip-changes", the Glue database "<db>" and the bucket "<p>-cdc-lake-123456789012"
    And every role of the recipe starts with "<p>-cdc-"

    Examples:
      | prefix    | p         | db            |
      | recipe-bo | recipe-bo | recipe_bo_cdc |
      | not set   | recipe    | recipe_cdc    |

  @R41
  Scenario Outline: The Region of every call
    Given the setting AWS_REGION is "<set>"
    When main.run runs with "--target aws"
    Then the exit code is 0
    And every call went to the Region "<used>"
    And the bucket was created in the Region "<used>"

    Examples:
      | set       | used         |
      | not set   | ca-central-1 |
      | ca-west-1 | ca-west-1    |

  @R57
  Scenario: Athena's result files expire in a bucket the recipe created
    When main.run runs with "--target aws"
    Then the bucket "recipe-ana-cdc-lake-123456789012" has one lifecycle rule: objects under "athena-results/" expire after 7 days

  @R57
  Scenario: A bucket that was passed in keeps its own rules
    Given the bucket "team-shared-bucket" exists and is passed in as BUCKET
    When main.run runs with "--target aws"
    Then the exit code is 0
    And no lifecycle rule was put on any bucket
