Feature: The run: updates through the stream into the table, and their lag
  main/run.py. With --target aws it is called inside moto; Lambda is the stand-in of features/environment.py, which
  runs the package the recipe deployed in this process, in batches, with Lambda's partial-batch retries but without
  its splitting of a failed batch: the number of updates stored in a failing scenario is this stand-in's.
  README, "Requirements": R10 to R13, R30 to R37, R40 to R46, R50, R51.

  @R10
  Scenario: What the producer puts on the stream
    When main/run.py runs with "--target aws --updates 30 --keep"
    Then the exit code is 0
    And every record put on the stream "recipe-ana-updates" has its driver_id as partition key
    And every record carries an update_id, an event_time and a sent_time at or after it

  @R11
  Scenario: More updates than one call takes
    When main/run.py runs with "--target local --updates 1201"
    Then the exit code is 0
    And the PutRecords calls carry "500, 500, 201" records
    And the output holds "updates stored: 1201"

  @R12
  Scenario: The stream refuses 3 records of the first call, once
    Given the stream refuses the first 3 records of its first 1 calls
    And the producer's random waits come out at the top of their range
    When main/run.py runs with "--target aws --updates 20 --rate 0 --keep"
    Then the exit code is 0
    And the PutRecords calls carry "20, 3" records
    And the output holds "sent: 20, retried by the producer: 3"
    And the output holds "updates stored: 20"
    And the producer waited 1 times between its calls, each a random time from 0 up to the limits "0.1" seconds

  @R13
  Scenario: The stream keeps refusing 3 records
    Given the stream refuses the first 3 records of every call
    And the producer's random waits come out at the top of their range
    When main/run.py runs with "--target aws --updates 20 --rate 0"
    Then the run stops with the error "3 records still refused after 5 attempts"
    And the PutRecords calls carry "20, 3, 3, 3, 3" records
    And the producer waited 4 times between its calls, each a random time from 0 up to the limits "0.1, 0.2, 0.4, 0.8" seconds
    And the account has nothing of the recipe left

  @R30
  Scenario: The report of a run
    When main/run.py runs with "--target local"
    Then the exit code is 0
    And the output holds "sent: 300, retried by the producer: 0"
    And the output holds "updates stored: 300"
    And the report has a median and a p95 for "lag: event to stored", "event to entered (before the broker)" and "entered to stored (broker and consumer)"
    And in every part the median is at most the p95

  @R31 @R32
  Scenario: Median and p95 of updates whose event times are known
    Given a file of 20 updates whose event times are 1, 2, ... 20 minutes before now
    When main/run.py runs with that file and "--target local"
    Then the exit code is 0
    And the output holds "updates stored: 20"
    And the median of "lag: event to stored" is 10 minutes, within 5 seconds
    And the p95 of "lag: event to stored" is 19 minutes, within 5 seconds

  @R33
  Scenario: A run on AWS removes what it created
    When main/run.py runs with "--target aws --updates 30"
    Then the exit code is 0
    And the output holds "updates stored: 30"
    And the account has nothing of the recipe left

  @R34
  Scenario: --keep leaves the pipeline
    When main/run.py runs with "--target aws --updates 30 --keep"
    Then the exit code is 0
    And the account has the stream "recipe-ana-updates", the table "recipe-ana-updates", the function "recipe-ana-consumer", the queue "recipe-ana-consumer-failures" and the role "recipe-ana-consumer-role"

  @R35
  Scenario: Some updates are not stored in time
    Given DynamoDB refuses to store the updates "u-7-000004, u-7-000005"
    When main/run.py runs with "--target aws --updates 10 --rate 0 --wait 20"
    Then the exit code is 1
    And the stand-in gave up on the records from sequence number "4" to "10" and sent their place to the queue "recipe-ana-consumer-failures"
    And the output holds "only 3 of 10 stored after 20 s: check the function's logs and the queue recipe-ana-consumer-failures"
    And the recipe looked at the table every 5 seconds while it waited
    And the output holds "updates stored: 3"
    And the account has nothing of the recipe left

  @R35
  Scenario: A file that holds one update twice
    Given a file of 10 updates in which the update "file-3" comes twice
    When main/run.py runs with that file and "--target aws --rate 0 --wait 20"
    Then the exit code is 0
    And the output holds "sent: 11"
    And the output holds "updates stored: 10"

  @R35
  Scenario: No update is stored at all
    Given the function "recipe-ana-consumer" fails on every batch
    When main/run.py runs with "--target aws --updates 10 --rate 0 --wait 20"
    Then the exit code is 1
    And the output holds "only 0 of 10 stored"
    And the output holds "updates stored: 0"

  @R36
  Scenario: The stream of an earlier run is still being deleted
    Given the stream "recipe-ana-updates" is in the DELETING state until the recipe has waited for it
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the exit code is 0
    And the recipe asked for the stream 2 times, and looked whether it was gone in between

  @R37
  Scenario: The local run needs no account
    Given no AWS credentials and no Region in the environment
    When main/run.py runs with "--target local --updates 50"
    Then the exit code is 0
    And the output holds "updates stored: 50"
    And no call was made to Lambda, IAM or SQS

  @R40
  Scenario: The stream
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the stream "recipe-ana-updates" was created with 1 shard, in provisioned mode

  @R41
  Scenario: How Lambda reads the stream
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the event source mapping starts at "TRIM_HORIZON", with batches of 100 records or 1 second
    And a failed batch is split and tried again at most 3 times, and records older than 3600 seconds are given up
    And what Lambda gives up on goes to the queue "recipe-ana-consumer-failures"

  @R45
  Scenario: What Lambda gave up on is kept for two weeks
    When main/run.py runs with "--target aws --updates 10 --rate 0 --keep"
    Then the queues "recipe-ana-consumer-failures" keep their messages 14 days

  @R42
  Scenario: What the function's role may do
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the role "recipe-ana-consumer-role" may read the stream "recipe-ana-updates", put items into the table "recipe-ana-updates", send to the queue "recipe-ana-consumer-failures" and write the log group "/aws/lambda/recipe-ana-consumer"
    And no statement of the role's policy has "*" as its resource or in an action
    And the role's policy has no other statement

  @R43
  Scenario: A role that is passed in
    Given LAMBDA_ROLE_ARN names the role "team-lambda" of the account
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the exit code is 0
    And no role was created
    And the function "recipe-ana-consumer" runs as "arn:aws:iam::123456789012:role/team-lambda"

  @R44
  Scenario: The function that was deployed
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the function "recipe-ana-consumer" was created with "python3.12" on "arm64", 128 MB and a timeout of 30 seconds
    And its package holds only "consumer.py", the same bytes as main/consumer.py
    And the function ran 1 time with a batch of 10 records, from that package

  @R46
  Scenario: The function's log group
    When main/run.py runs with "--target aws --updates 10 --rate 0 --keep"
    Then the log groups "/aws/lambda/recipe-ana-consumer" were made before their functions and keep their lines 7 days

  @R50
  Scenario Outline: Every name starts with PREFIX
    Given the setting PREFIX is "<prefix>"
    When main/run.py runs with "--target aws --updates 10 --rate 0 --keep"
    Then every stream, table, function, queue, role and log group of the account starts with "<start>"

    Examples:
      | prefix    | start      |
      | recipe-bo | recipe-bo- |
      | not set   | recipe-    |

  @R51
  Scenario Outline: The Region of every call
    Given the setting AWS_REGION is "<set>"
    When main/run.py runs with "--target aws --updates 10 --rate 0"
    Then the exit code is 0
    And every call went to the Region "<used>"

    Examples:
      | set       | used         |
      | not set   | ca-central-1 |
      | ca-west-1 | ca-west-1    |
