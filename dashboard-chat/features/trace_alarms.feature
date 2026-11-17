Feature: Extension: trace analysis and alarms
  One span per model call and per tool call, linked to the span of its request; four metrics per question;
  three CloudWatch alarms; DuckDB queries over the spans.
  README, "Requirements": R60, R100 to R104; ext-trace-alarms/README.md.
  Scenarios tagged @aws run against moto, a local stand-in for AWS.

  Background:
    Given the shop's tables are loaded in DuckDB
    And the stub LLM with the fixed replies of main/stub_replies.json

  @R100
  Scenario: The spans of one answered question
    When customer "c-0007" asks "What were total sales in Vancouver?"
    And the spans of that answer are written to a local file
    Then the span ids are "0000, 0001, 0002, 0003"
    And the span kinds are "request, model_call, tool_call, model_call"
    And every span after the first has parent_id "0000"

  @aws @R100 @R60
  Scenario: One Query returns the spans of a request in order
    Given the spans table of a run with "--target aws"
    When customer "c-0007" asks "What were total sales in Vancouver?"
    And the spans of that answer are written to the DynamoDB table "test-chat-spans"
    Then one Query by the request id returns the span ids "0000, 0001, 0002, 0003"

  @aws @R101
  Scenario: Four metrics per question
    When customer "c-0007" asks "How many orders are there?"
    And the metrics of that answer are sent to CloudWatch
    Then the CloudWatch namespace "test/dashboard-chat" has the metrics "CostCAD, Errors, LatencyMs, Requests"

  @aws @R102 @R60
  Scenario: Three alarms that notify one topic
    When the alarms are created with the email address "ana@example.com"
    Then the alarms of the account are "test-chat-daily-cost, test-chat-errors, test-chat-slow-answers"
    And every alarm notifies the SNS topic "test-chat-alarms"
    And every alarm compares with "GreaterThanOrEqualToThreshold" and treats missing data as "notBreaching"
    And the alarms have these settings:
      | alarm                  | metric    | statistic | threshold | period | periods |
      | test-chat-errors       | Errors    | Sum       | 3         | 300    | 1       |
      | test-chat-slow-answers | LatencyMs | p90       | 15000     | 300    | 3       |
      | test-chat-daily-cost   | CostCAD   | Sum       | 0.25      | 86400  | 1       |

  @aws @R103
  Scenario: A span write and a metric call that AWS refuses
    When customer "c-0007" asks "How many orders are there?"
    And the spans of that answer are written to the DynamoDB table "test-chat-spans", which does not exist
    And the metrics of that answer are sent to a CloudWatch client that refuses the call
    Then the status is "answered"
    And the printed output holds "span write failed"
    And the printed output holds "put_metric_data failed"

  @R104
  Scenario: The four tables of the analysis
    When customer "c-0007" asks "How many orders are there?"
    And the spans of that answer are written to a local file
    And customer "c-0007" asks "What is the weather today?"
    And the spans of that answer are written to a local file
    And analyze.py reads that file
    Then the printed output holds "per request"
    And the printed output holds "time by kind of call"
    And the printed output holds "calls per request"
    And the printed output holds "slowest requests, with their calls"
    And the table "calls per request" counts 1 request with 1 call and 1 request with 3 calls
