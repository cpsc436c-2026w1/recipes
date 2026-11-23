Feature: Every resource on one list, what it costs, and nothing left billing
  The team member's three stories: every resource on one list, what a run, a month and a year cost, and nothing
  left billing. Offline, moto stands in for AWS in the account 111122223333 (features/support.py stands in for the
  Tagging API's answer for Kinesis, which moto does not give); PREFIX and TEAM are t00, the owner ana. The rates are
  those of prices.toml, the course price sheet read on 2026-11-18. The data sets are described in README, Data.

  @R10
  Rule: Every resource a script creates carries the course tags where it can, and has a row in the ledger

    Example: The demonstration system is created
      Given no bucket t00-lake-111122223333 yet
      When scripts/demo_up.py creates the demonstration system
      Then each of these 8 resources was created with the tags Project=436c, Team=t00 and Owner=ana:
        | type                        | name                              |
        | S3 bucket                   | t00-lake-111122223333             |
        | Kinesis data stream         | t00-cost-events                   |
        | Lambda function             | t00-cost-count                    |
        | Lambda event source mapping | t00-cost-events to t00-cost-count |
        | CloudWatch log group        | /aws/lambda/t00-cost-count        |
        | DynamoDB table              | t00-cost-counts                   |
        | Athena workgroup            | t00-wg                            |
        | IAM role                    | t00-cost-role                     |
      And state/ledger.jsonl has 10 rows marked "created", the Glue database t00_cost and its table among them

  @R20
  Rule: The inventory finds each resource by every route and lists what is not in the ledger

    Example: The role through IAM, the Glue table by name, and a queue made by hand
      Given the demonstration system has been created
      And an SQS queue "t00-scratch" with the tag Team=t00 that is not in the ledger
      When scripts/inventory.py runs
      Then docs/resources.md has 10 rows and 1 row under "Not in the ledger": t00-scratch
      And the rows of t00-cost-events and t00-cost-count say "hourly" and "per use"
      And the row of t00-cost-role says it was found by "IAM" and "ledger"
      And the row of t00_cost.trip_events_demo says it was found by "name" and "ledger"

    Example: A bucket that existed before is borrowed
      Given the bucket t00-lake-111122223333 exists before scripts/demo_up.py runs
      When scripts/demo_up.py creates the demonstration system and scripts/inventory.py runs
      Then the row of t00-lake-111122223333 says "borrowed"

  @R30
  Rule: A run writes the units of each billed part, each marked with how it was found

    Example: Two calls of the counting function
      Given the function's log group has these REPORT lines:
        | request_id | billed_duration_ms | memory_mb | init_duration_ms |
        | rq-1       | 810                | 128       | 493.18           |
        | rq-2       | 25                 | 128       |                  |
      When the run ends
      Then units.json has 2 Lambda requests and 0.104375 billed GB-seconds, marked "measured"

    Example: A Glue run shorter than a minute
      Given a Glue run of 2 G.1X workers with ExecutionTime 45 s and no DPUSeconds
      When the run ends
      Then units.json has 0.0333 DPU-hours for the job, marked "computed"

    Example: A billed part without a reader
      Given the inventory lists an Application Load Balancer and the run did not read its LCUs
      When the run ends
      Then units.json has the row "not measured": "Application Load Balancer LCU-hours"

  @R40
  Rule: The cost of a run has one row for each billed part, in USD and in CAD

    Example: Units of a short run
      Given these units of a run:
        | part                     | units |
        | Lambda requests          | 1000  |
        | Lambda GB-seconds, arm64 | 50    |
        | DynamoDB write units     | 4000  |
        | Athena Data scanned, MB  | 3     |
        | Kinesis shard-hours      | 2     |
      When scripts/cost.py runs
      Then the cost rows are, in USD:
        | part                     | cost     |
        | Lambda requests          | 0.000200 |
        | Lambda GB-seconds, arm64 | 0.000667 |
        | DynamoDB write units     | 0.002750 |
        | Athena, billed 10 MB     | 0.000052 |
        | Kinesis shard-hours      | 0.033000 |
      And the total is 0.036669 USD and 0.053170 CAD
      And each row names its rate and "price sheet, ca-central-1, 2026-11-18"

    Example: A part whose rate is not in prices.toml
      Given the units of a run include 2000 Parameter Store API interactions
      When scripts/cost.py runs
      Then the cost row of Parameter Store API interactions says "unpriced"
      And the total is called a subtotal

  @R50
  Rule: The cost of 30 days is given always on and for demonstrations only

    Example: One provisioned shard and a run a day
      Given a stream of 1 provisioned shard
      And the parts of a run that are not hourly cost 0.003118 USD
      And the stated rate is one run a day and 10 demonstration hours in 30 days
      When scripts/cost.py runs
      Then the 30 days always on cost 11.9735 USD, of which 11.8800 USD is the shard
      And the 30 days for demonstrations only cost 0.1962 USD, of which 0.1650 USD is the shard

  @R60
  Rule: The horizon is a year from busy and idle months, with the month that passes the cost limit

    Example: The demonstration system over a year
      Given a stream of 1 provisioned shard and parts of a run that are not hourly at 0.003118 USD
      And 4 busy months of 30 days with a run a day, 245 idle days, and a cost limit of 30 USD
      When scripts/cost.py runs
      Then the year always on costs 144.91 USD and 210.13 CAD, and passes the cost limit in the 3rd month
      And the year for demonstrations only costs 0.78 USD and 1.14 CAD, and does not pass it

  @R70
  Rule: The teardown runs the inventory again, then each clean-up step, hourly parts first and the bucket last

    Example: Teardown of the demonstration system
      Given the demonstration system and its inventory
      When scripts/teardown.sh runs
      Then the first delete is of stream t00-cost-events
      And the last step is scripts/lake.py down
      And each of the 10 resources is asked for by its own service after the deletes

    Example: A stream with the team's tag that is not in the ledger is kept without --tagged
      Given the demonstration system and its inventory
      And a stream t00-extra-events created afterwards with the tag Team=t00
      When scripts/teardown.sh runs
      Then stream t00-extra-events is still there
      And it prints "FAILED: still there: Kinesis data stream t00-extra-events"
      And it exits with code 1

    Example: With --tagged, a stream found only by the team's tag is deleted
      Given the demonstration system and its inventory
      And a stream t00-extra-events created afterwards with the tag Team=t00
      When scripts/teardown.sh runs with --tagged
      Then stream t00-extra-events is deleted

    Example: A borrowed bucket is kept
      Given the bucket t00-lake-111122223333 exists before scripts/demo_up.py runs
      And the demonstration system and its inventory
      When scripts/teardown.sh runs
      Then the bucket still exists

  @R71
  Rule: A resource that is still there fails the teardown

    Example: The bucket delete is refused
      Given the demonstration system and its inventory
      And the delete of bucket t00-lake-111122223333 is refused with AccessDenied
      When scripts/teardown.sh runs
      Then it prints "FAILED: still there: S3 bucket t00-lake-111122223333"
      And it exits with code 1

  @R72
  Rule: After a full teardown the inventory shows each resource as deleted

    Example: Every service answers "not found"
      Given the demonstration system and its inventory
      When scripts/teardown.sh runs
      Then each of the 10 rows of docs/resources.md says "deleted" with the time its service answered "not found"
      And it exits with code 0

  @R73
  Rule: A resource of a type the teardown does not know is left for a person

    Example: An AppSync API with the team's tag
      Given the demonstration system and its inventory
      And an AppSync API "t00-scratch-api" that carries the tag Team=t00
      When scripts/teardown.sh runs
      Then docs/resources.md lists "t00-scratch-api" under "Delete by hand" with its ARN
      And the API is still there
      And it exits with code 1

  @R74
  Rule: A refused inventory route makes the teardown incomplete

    Example: GetResources is refused
      Given the demonstration system and its inventory
      And the tagging API's GetResources is refused with AccessDenied
      When scripts/teardown.sh runs
      Then it prints "FAILED: inventory incomplete: route tag refused"
      And it exits with code 1
