Feature: ext-sdk-lambda, a question about one day of trips answered by an agent on Lambda
  The analyst sends a question to a function URL; an agent built with Strands or Pydantic AI answers it with one
  read-only SQL tool over one day of trips. Offline examples run the handler with scripted model replies on the
  synthetic stand-in for SIM01 (make_trips.py --synthetic: the same counts); the refusal examples run the real
  model classes against a stand-in server; @live examples call the deployed function URL (LIVE=1, in the sandbox).

  @R110
  Rule: A question sent to the function URL is answered with the value and the SQL and rows from the tool log

    Example: Trips of the whole day
      Given the function ships the simulated data set SIM01 "three-zone day"
      And AGENT_SDK is "strands"
      And the model's replies are:
        | reply     | content                                              |
        | tool call | run_sql: SELECT COUNT(*) AS trips FROM trips         |
        | output    | {"reply": "There were 19074 trips.", "value": 19074} |
      When the analyst sends "How many trips were requested on 2024-03-14?" to the function
      Then the status is "answered"
      And the value is 19074
      And the SQL is "SELECT COUNT(*) AS trips FROM trips"
      And the rows are "trips: 19074"

    @live
    Example: Response time of the eval's questions
      Given the function "<PREFIX>-agent-sdk" ships the simulated data set SIM01 "three-zone day"
      When invoke_url.py --measure sends the 12 questions of the eval "trip-questions" 3 times each, one at a time
      Then the 35th of the 36 response times, in order, is under 30 s

  @R111
  Rule: A value that no query of the question returned is not shown

    Example: The model rounds the number it was given
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                         |
        | tool call | run_sql: SELECT COUNT(*) FROM trips             |
        | output    | {"reply": "About 19000 trips.", "value": 19000} |
      When the analyst sends "How many trips were requested on 2024-03-14?" to the function
      Then the status is "did_not_answer"
      And the reason is "value_not_in_result"
      And there is no value

    Example: The model writes its own arithmetic as a query
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                                      |
        | tool call | run_sql: SELECT 7661 - 7068 AS difference                                    |
        | output    | {"reply": "There were 593 more trips in zone 161 than in 230.", "value": 593} |
      When the analyst sends "How many more trips were requested in zone 161 than in zone 230 on the whole day?" to the function
      Then the status is "did_not_answer"
      And the reason is "value_not_in_result"
      And there is no value

    Example: The difference of two counts the tool returned
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                                           |
        | tool call | run_sql: SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 161               |
        | tool call | run_sql: SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 230               |
        | output    | {"reply": "There were 593 more trips in zone 161 than in zone 230.", "value": 593} |
      When the analyst sends "How many more trips were requested in zone 161 than in zone 230 on the whole day?" to the function
      Then the status is "answered"
      And the value is 593

    Example: A number no result holds or gives
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                                           |
        | tool call | run_sql: SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 161               |
        | tool call | run_sql: SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 230               |
        | output    | {"reply": "There were 600 more trips in zone 161 than in zone 230.", "value": 600} |
      When the analyst sends "How many more trips were requested in zone 161 than in zone 230 on the whole day?" to the function
      Then the status is "did_not_answer"
      And the reason is "value_not_in_result"
      And there is no value

  @R112
  Rule: An output without a value is reported as "did not answer"

    Example: A question the day's trips cannot answer
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply  | content                                                  |
        | output | {"reply": "The trips table has no tips.", "value": null} |
      When the analyst sends "What did riders tip in zone 161?" to the function
      Then the status is "did_not_answer"
      And the reason is "no_value"
      And the reply is "The trips table has no tips."

  @R113
  Rule: A reply that does not state the checked value, or holds a number no query gave, is not shown

    Example: The value is right but the reply rounds it
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                    |
        | tool call | run_sql: SELECT COUNT(*) AS trips FROM trips               |
        | output    | {"reply": "There were about 19000 trips.", "value": 19074} |
      When the analyst sends "How many trips were requested on 2024-03-14?" to the function
      Then the status is "did_not_answer"
      And the reason is "reply_disagrees"
      And there is no value

    Example: Numbers from the question and the SQL are allowed
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                                                  |
        | tool call | run_sql: SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 AND hour(requested_time) = 8 |
        | output    | {"reply": "191 trips were requested in zone 161 between 08:00 and 09:00.", "value": 191}  |
      When the analyst sends "How many trips were requested in zone 161 between 08:00 and 09:00?" to the function
      Then the status is "answered"
      And the value is 191

  @R120
  Rule: The one tool runs only one SELECT over trips, read-only

    Example: Statements the tool refuses
      Given the function ships the simulated data set SIM01 "three-zone day"
      When the agent calls run_sql with each of these statements:
        | sql                                          |
        | DELETE FROM trips                            |
        | SELECT COUNT(*) FROM trips; DROP TABLE trips |
        | SELECT * FROM read_csv('/etc/passwd')        |
        | COPY trips TO '/tmp/trips.csv'               |
      Then each of the 4 tool results is a rejection
      And DuckDB ran 0 statements
      And table trips still has 19074 rows

    Example: A SELECT that does not read trips
      Given the function ships the simulated data set SIM01 "three-zone day"
      When the agent calls run_sql with each of these statements:
        | sql                                                    |
        | SELECT 7661 - 7068 AS difference                       |
        | WITH t AS (SELECT 593 AS n) SELECT n FROM t            |
        | WITH trips AS (SELECT 19074 AS n) SELECT n FROM trips  |
      Then each of the 3 tool results is a rejection
      And DuckDB ran 0 statements

    Example: A column of constants beside a read of trips
      Given the function ships the simulated data set SIM01 "three-zone day"
      When the agent calls run_sql with each of these statements:
        | sql                                                                     |
        | SELECT 7661 - 7068 AS difference FROM trips LIMIT 1                     |
        | SELECT 161 AS zone, COUNT(*) AS n FROM trips WHERE pickup_zone_id = 161 |
      Then each of the 2 tool results is a rejection
      And DuckDB ran 0 statements

    Example: A computed number read from trips in subqueries
      Given the function ships the simulated data set SIM01 "three-zone day"
      When the agent calls run_sql with each of these statements:
        | sql                                                                                                                               |
        | SELECT (SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161) - (SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 230) AS more |
      Then the tool result's rows are "593"

  @R130
  Rule: Where Pydantic AI is chosen, the same tool and prompt give a typed answer

    Example: The same question with each SDK
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies of the example "Trips of the whole day"
      When the analyst sends "How many trips were requested on 2024-03-14?" with each AGENT_SDK:
        | AGENT_SDK   | status   | value |
        | strands     | answered | 19074 |
        | pydantic-ai | answered | 19074 |
      Then each answer has the status and value in its row
      And the pydantic-ai answer validates as TripAnswer

  @R140
  Rule: App code stops a question before a 5th model call or a 4th run_sql call, and at 25 seconds whatever is running

    Example: A model that never stops calling the tool
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model replies with a tool call run_sql "SELECT COUNT(*) FROM trips" every time
      When the analyst sends "How many trips?" with each AGENT_SDK:
        | AGENT_SDK   | status  | model_calls | tool_calls |
        | strands     | stopped | 4           | 3          |
        | pydantic-ai | stopped | 4           | 3          |
      Then each answer has the status and counts in its row

    Example: A question that needs 3 queries is answered with either SDK
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                        |
        | tool call | run_sql: SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 48  |
        | tool call | run_sql: SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 |
        | tool call | run_sql: SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 230 |
        | output    | {"reply": "Zone 161 had the most trips: 7661.", "value": 7661} |
      When the analyst sends "Which of the three zones had the most trips?" with each AGENT_SDK:
        | AGENT_SDK   | status   | value | model_calls | tool_calls |
        | strands     | answered | 7661  | 4           | 3          |
        | pydantic-ai | answered | 7661  | 4           | 3          |
      Then each answer has the status, value and counts in its row
      And the SQL of each answer is "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161"

    Example: A model that is too slow
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the clock runs 20 times faster than the wall clock
      And the model's replies are:
        | reply     | content                             | takes |
        | tool call | run_sql: SELECT COUNT(*) FROM trips | 12 s  |
        | tool call | run_sql: SELECT COUNT(*) FROM trips | 12 s  |
        | tool call | run_sql: SELECT COUNT(*) FROM trips | 12 s  |
      When the analyst sends "How many trips?" with each AGENT_SDK:
        | AGENT_SDK   | status  | model_calls |
        | strands     | stopped | 3           |
        | pydantic-ai | stopped | 3           |
      Then each answer has the status and count in its row
      And the third model call, started at 24 s, was cancelled at 25 s
      And each question ended within 26 s

  @R150
  Rule: A refusal of the course LLM gateway is reported as "did not answer", without a second request

    Example: Refusals the gateway can send
      Given the function ships the simulated data set SIM01 "three-zone day"
      And a stand-in server that answers the first model call like the course LLM gateway
      When it answers with each of these:
        | AGENT_SDK   | http_status | code                  | retry_after |
        | pydantic-ai | 429         | team_budget_exhausted |             |
        | pydantic-ai | 429         | rate_limit_exceeded   | 60          |
        | pydantic-ai | 401         | invalid_api_key       |             |
        | pydantic-ai | 403         | model_not_allowed     |             |
        | pydantic-ai | 502         | upstream_error        |             |
        | strands     | 429         | team_budget_exhausted |             |
        | strands     | 429         | rate_limit_exceeded   | 60          |
        | strands     | 403         | key_expired           |             |
        | strands     | 502         | upstream_error        |             |
      Then each answer has status "did_not_answer", no value, and the status, code and retry_after of its row
      And the stand-in server received 1 request for each question

  @R160
  Rule: An output that is not a TripAnswer after one retry is not shown as an answer

    Example: The model replies in prose twice
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply  | content                   |
        | output | about nineteen thousand   |
        | output | roughly 19 thousand trips |
      When the analyst sends "How many trips?" with each AGENT_SDK:
        | AGENT_SDK   | status         | reason        |
        | strands     | did_not_answer | output_format |
        | pydantic-ai | did_not_answer | output_format |
      Then each answer has the status and reason in its row
      And no answer has a value

  @R170
  Rule: Only signed requests from allowed identities reach the function

    Example: The deploy makes the function URL with auth type AWS_IAM
      Given a stand-in for the team sandbox with the package built
      When ext-sdk-lambda/deploy.py up runs
      Then the function URL of "test-agent-sdk" has auth type "AWS_IAM"
      And the function is arm64, Python 3.12, 1024 MB, 60 s, with the token parameter's name and not the token

    @live
    Example: Signed and unsigned requests
      Given the function URL of "<PREFIX>-agent-sdk" with auth type AWS_IAM
      When these requests are sent:
        | signed by              | http_status |
        | nobody                 | 403         |
        | the deploying identity | 200         |
      Then each request gets the status in its row
      And the function's log has 1 invocation

  @R171
  Rule: Every resource the deploy makes carries the course tags and a ledger line

    Example: Tags and ledger
      Given a stand-in for the team sandbox with the package built
      When ext-sdk-lambda/deploy.py up runs
      Then the function, the role, the log group and the parameter carry the tags Project, Team and Owner
      And the ledger records "created" for the function, its URL, the role, the log group and the parameter

  @R180
  Rule: A question about a zone and a time of the day gets the value it asks for, with either SDK

    Example: One zone in one hour
      Given the function ships the simulated data set SIM01 "three-zone day"
      And the model's replies are:
        | reply     | content                                                                                  |
        | tool call | run_sql: SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 AND hour(requested_time) = 8 |
        | output    | {"reply": "191 trips were requested in zone 161 between 08:00 and 09:00.", "value": 191}  |
      When the analyst sends "How many trips were requested in zone 161 between 08:00 and 09:00?" with each AGENT_SDK:
        | AGENT_SDK   | status   | value |
        | strands     | answered | 191   |
        | pydantic-ai | answered | 191   |
      Then each answer has the status and value in its row

    @live @eval
    Example: Eval of trip questions
      Given the 12 cases of the eval "trip-questions"
      When the model that the system uses answers each case 3 times with each AGENT_SDK
      Then at least 29 of the 36 runs are correct for each AGENT_SDK
      And at least 10 of the 12 cases pass for each AGENT_SDK

  @R190
  Rule: The clean-up deletes what the deploy made and leaves the lake bucket

    Example: Clean-up after a deploy
      Given a stand-in for the team sandbox with the package built
      And ext-sdk-lambda/deploy.py up has run
      When ext-sdk-lambda/clean_up.py --yes runs
      Then no function, role, log group or parameter of "test-agent-sdk" is left
      And the lake bucket is still there, without objects under "code/agent-sdk/"
      And a second clean-up reports every resource "absent" and exits with code 0
