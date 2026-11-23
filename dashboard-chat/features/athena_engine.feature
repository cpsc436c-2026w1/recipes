@aws
Feature: ext-athena-engine: chat questions answered by Athena, within limits set outside the model
  The requirements are in ext-athena-engine/README.md. Offline, the query function runs in-process against a
  stand-in for Athena (the same SELECT in DuckDB over the same Parquet files) and moto's DynamoDB; SIM01 is a
  synthetic table with SIM01's counts. The model's tool calls are scripted; "the model's tool calls are" is one
  reply of the model with these calls, after which it replies with the first row's last value. @live examples run
  in AWS after ext-athena-engine/deploy.py (LIVE=1 uv run behave -t @live), and are skipped otherwise.

  @R130
  Rule: A question the model answers with a query is answered from the rows Athena returns

    Scenario: Trips of one zone on one day
      Given table trips holds the simulated data set SIM01 "three-zone day"
      And the model's tool calls are:
        | tool    | sql                                                                                                  |
        | run_sql | SELECT COUNT(*) AS trips FROM trips WHERE pickup_zone_id = 161 AND requested_day = DATE '2024-03-14' |
      When the analyst asks the Athena chat "How many trips were requested in zone 161 on 2024-03-14?"
      Then the status is "answered"
      And the rows are "trips: 7661"
      And the query ran in workgroup "<PREFIX>-chat-wg" with database "<prefix>_chat"

    @live
    Scenario: The same question in AWS
      Given table trips holds the simulated data set SIM01 "three-zone day"
      And the model's tool calls are:
        | tool    | sql                                                                                                  |
        | run_sql | SELECT COUNT(*) AS trips FROM trips WHERE pickup_zone_id = 161 AND requested_day = DATE '2024-03-14' |
      When the analyst asks the Athena chat "How many trips were requested in zone 161 on 2024-03-14?"
      Then the rows are "trips: 7661"
      And the query ran in workgroup "<PREFIX>-chat-wg" with database "<prefix>_chat"

    @live
    Scenario: Response time of the eval's questions
      Given table trips holds DS01
      When run_athena.py --measure asks the 10 questions of the eval "trip-counts" 3 times each, one at a time
      Then the 29th of the 30 response times, in order, is under 20 s

  @R140
  Rule: A statement other than one SELECT over trips and zones is refused and never reaches Athena

    Scenario: Writes, second statements, other tables and CTEs that hide a name
      Given table trips holds the simulated data set SIM01 "three-zone day"
      And the model's tool calls are:
        | tool    | sql                                                                                |
        | run_sql | DROP TABLE trips                                                                   |
        | run_sql | CREATE TABLE trips_copy AS SELECT * FROM trips                                     |
        | run_sql | UNLOAD (SELECT * FROM trips) TO 's3://recipe-lake/athena-results/chat-wg/x/'       |
        | run_sql | SELECT COUNT(*) FROM trips; DELETE FROM trips                                      |
        | run_sql | SELECT table_name FROM information_schema.tables                                   |
        | run_sql | SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret) |
      When the analyst asks the Athena chat "Clean up the trips table"
      Then each of the 6 tool results of the Athena chat is a rejection
      And Athena received 0 queries

    @live
    Scenario: The same statements in AWS
      Given table trips holds DS01
      And the model's tool calls are:
        | tool    | sql                                                                                |
        | run_sql | DROP TABLE trips                                                                   |
        | run_sql | CREATE TABLE trips_copy AS SELECT * FROM trips                                     |
        | run_sql | UNLOAD (SELECT * FROM trips) TO 's3://recipe-lake/athena-results/chat-wg/x/'       |
        | run_sql | SELECT COUNT(*) FROM trips; DELETE FROM trips                                      |
        | run_sql | SELECT table_name FROM information_schema.tables                                   |
        | run_sql | SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret) |
      When the analyst asks the Athena chat "Clean up the trips table"
      Then each of the 6 tool results of the Athena chat is a rejection
      And Athena received 0 queries

    Scenario: A CTE over the allowed tables is let through
      Given table trips holds the simulated data set SIM01 "three-zone day"
      And the model's tool calls are:
        | tool    | sql                                                                                        |
        | run_sql | WITH z AS (SELECT * FROM trips WHERE pickup_zone_id = 161) SELECT COUNT(*) AS trips FROM z |
      When the analyst asks the Athena chat "How many trips were requested in zone 161?"
      Then Athena received 1 query
      And the rows are "trips: 7661"

  @R150
  Rule: A query past the scan limit is cancelled by the workgroup, and the model is told why

    Scenario: A query that would scan 300 MB, offline
      Given table trips holds the simulated data set SIM01 "three-zone day", and every query would scan 300 MB
      And the model's tool calls are:
        | tool    | sql                 |
        | run_sql | SELECT * FROM trips |
      When the analyst asks the Athena chat "Show me all the trips"
      Then Athena cancels the query
      And the tool result is "the query was stopped at the scan limit of 100 MB"
      And the question's trace has billed Data scanned 100 MB

    @live
    Scenario: Every column of the month
      Given table trips holds DS01
      And the model's tool calls are:
        | tool    | sql                 |
        | run_sql | SELECT * FROM trips |
      When the analyst asks the Athena chat "Show me all the trips"
      Then Athena cancels the query
      And the tool result is "the query was stopped at the scan limit of 100 MB"

  @R160
  Rule: A question runs at most 3 queries, also when the model asks for more in one reply

    Scenario: Queries past the limit in one reply of the model
      Given table trips holds the simulated data set SIM01 "three-zone day"
      And the model's tool calls are:
        | tool    | sql                                                   |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 48  |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 230 |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 100 |
      When the analyst asks the Athena chat "How many trips did each zone have?"
      Then Athena received 3 queries
      And the 4th tool result is "query limit reached"

  @R170
  Rule: The reader role can only read the two tables, in the workgroup that has the scan limit

    Scenario: The role's policy, offline
      When the policy of the role "<PREFIX>-chat-reader" is written for the bucket "<PREFIX>-lake-111122223333"
      Then it allows athena:StartQueryExecution only on the workgroup "<PREFIX>-chat-wg"
      And it allows no Glue action that creates, changes or deletes a table
      And it allows s3:PutObject only under "athena-results/chat-wg/"

    @live
    Scenario: The query function runs statements without the chat's parse check
      Given the function "<PREFIX>-chat-query" with its role "<PREFIX>-chat-reader"
      When it is invoked directly with each of these queries:
        | workgroup        | sql                              | answer        |
        | <PREFIX>-chat-wg | DROP TABLE trips                 | access denied |
        | <PREFIX>-chat-wg | CREATE TABLE t2 AS SELECT 1 AS x | access denied |
        | primary          | SELECT COUNT(*) FROM trips       | access denied |
        | <PREFIX>-chat-wg | SELECT COUNT(*) FROM trips       | succeeded     |
      Then each query gets the answer in its row
      And table trips still exists

  @R180
  Rule: The trace item of a question has its Data scanned and its cost

    Scenario: Queries under the minimum, over it, and failed
      Given the queries of a question ended like this:
        | query | state     | data_scanned_mb |
        | 1     | SUCCEEDED | 3               |
        | 2     | SUCCEEDED | 40              |
        | 3     | FAILED    | 0               |
      When the question ends
      Then its trace item has billed Data scanned 50 MB
      And its Athena cost is 0.000262 USD
      And its model calls' cost is in CAD

  @R190
  Rule: A question about a zone and a time is answered with the rows that count it

    Scenario: The rows of the last query that succeeded are compared, typed and in any order
      Given the reference rows of a case are:
        | pickup_zone_id | trips |
        | 48             | 4345  |
        | 161            | 7661  |
      And the last query that succeeded returned, after its header row:
        | pickup_zone_id | trips |
        | 161            | 7661  |
        | 48             | 4345  |
      When the case is scored
      Then the run is correct

    Scenario: The eval's code, offline, with a model that writes the reference queries
      Given table trips holds the simulated data set SIM01 "three-zone day"
      When the scripted model answers each of the 10 cases of the eval "trip-counts" 3 times
      Then at least 24 of the 30 runs are correct
      And at least 8 of the 10 cases pass

    @live
    Scenario: Eval of trip counts
      Given table trips holds DS01
      When the model that the system uses answers each case of the eval "trip-counts" 3 times
      Then at least 24 of the 30 runs are correct
      And at least 8 of the 10 cases pass

  @R200
  Rule: A query that could take the day past 5 GB billed is not started

    Scenario: The daily cap
      Given table trips holds the simulated data set SIM01 "three-zone day"
      When a query is asked for with today's billed Data scanned at each of these:
        | billed today, MB | result                   |
        | 5000             | the query runs           |
        | 5030             | daily scan limit reached |
      Then each query gets the result in its row
      And Athena received 1 query

    @live
    Scenario: The daily cap in AWS
      Given table trips holds DS01
      When a query is asked for with today's billed Data scanned at each of these:
        | billed today, MB | result                   |
        | 5000             | the query runs           |
        | 5030             | daily scan limit reached |
      Then each query gets the result in its row
      And Athena received 1 query

  @R210
  Rule: A question ends by its deadline of 18 seconds, and a query still running is stopped

    Scenario: Athena never finishes
      Given table trips holds the simulated data set SIM01 "three-zone day", and Athena keeps every query running
      And the model's tool calls are:
        | tool    | sql                                                   |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 48  |
      When the analyst asks the Athena chat "How many trips started in zones 161 and 48?"
      Then Athena received 2 queries and 2 calls of StopQueryExecution
      And the status is "stopped"
      And the reason is "deadline"
      And the question ended within 20 s

    Scenario: A model call that starts near the deadline
      Given table trips holds the simulated data set SIM01 "three-zone day", and Athena takes 9 s for each query
      And the stand-in model takes 5 s for each reply
      And the model's tool calls are:
        | tool    | sql                                                   |
        | run_sql | SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 |
      When the analyst asks the Athena chat "How many trips were requested in zone 161?"
      Then the second model call starts at 14 s with a timeout of 4 s
      And the second model call is cut off at 18 s
      And the status is "stopped"
      And the reason is "deadline"
      And the question ended within 18.5 s

  @R220
  Rule: Clean-up deletes what the extension owns and keeps what it borrowed

    Scenario: The bucket and a borrowed workgroup stay
      Given the extension's resources exist, the workgroup "<PREFIX>-chat-wg" recorded borrowed
      When ext-athena-engine/clean_up.py --yes runs
      Then the function, its log group, the role, the usage table, the database and the prefix chat/ are gone
      And the bucket, the prefix raw/trip-events/ and the workgroup "<PREFIX>-chat-wg" are still there
