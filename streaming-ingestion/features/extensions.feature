Feature: The two extensions
  ext-windowed-aggregates/README.md, "Requirements": R70 to R76. ext-fan-out/README.md: R80 to R88.

  @R70 @R71 @R72
  Scenario: Counts per zone and window, with late records dropped and counted
    When windowed aggregates runs with "--target local"
    Then the exit code is 0
    And every printed window starts on a 5-minute boundary and is "final" or "open"
    And the output holds "truth 600 (rule: drop)"
    And the output holds "every window: count + late dropped = truth: yes"
    And the total counted plus the total late and dropped is 600, with more than 0 dropped
    And the windows that end after the newest event time minus 120 seconds are "open", the others "final"

  @R73
  Scenario: Late records corrected and counted
    When windowed aggregates runs with "--target local --late-rule correct"
    Then the output holds "every window: count = truth: yes"
    And the total counted is 600, with more than 0 corrections and 0 dropped

  @R71
  Scenario: A longer close delay leaves fewer records late
    When windowed aggregates runs with "--target local --close-delay 120"
    And the dropped total is noted
    And windowed aggregates runs with "--target local --close-delay 600"
    Then the dropped total is smaller than the one noted

  @R74 @R76 @R45 @R46
  Scenario: The window function on AWS, as a second consumer of the stream
    When windowed aggregates runs with "--target aws --late-rule drop --close-delay 120 --keep"
    Then the exit code is 0
    And the function "recipe-ana-windows" was created from "windows.py" alone, with LATE_RULE "drop" and CLOSE_DELAY_S "120"
    And its event source mapping reads the stream "recipe-ana-updates" in batches of 25
    And the queues "recipe-ana-windows-failures" keep their messages 14 days
    And the log groups "/aws/lambda/recipe-ana-windows" were made before their functions and keep their lines 7 days
    And the output holds "every window: count + late dropped = truth: yes"

  @R74
  Scenario: A batch that is delivered twice is counted once
    Given the window table of PREFIX "recipe-ana" exists and the window function is told its name
    And 40 updates of one zone, 10 seconds apart
    When the window function receives them, and then receives them again
    Then the windows hold 40 updates in all
    And the function's last log line counts 40 repeats

  @R75
  Scenario: Markers expire after two days
    Given the window table of PREFIX "recipe-ana" exists and the window function is told its name
    And 40 updates of one zone, 10 seconds apart
    When the window function receives them at 1800000500 seconds
    Then each of the 40 markers has "expires_at" 172800 seconds after 1800000500

  @R75
  Scenario: The window table deletes expired markers by itself
    When windowed aggregates runs with "--target aws --keep"
    Then time to live is on for "expires_at" in the table "recipe-ana-windows"

  @R80 @R81 @R82
  Scenario: A stopped consumer does not hold up the other, offline
    When fan-out runs with "--target local --updates 60 --stopped-for 0"
    Then the exit code is 0
    And the output holds "while history was stopped: dashboard stored 60, history stored 0, history queue holds 60"
    And the report of "dashboard consumer" and of "history consumer" each say "updates stored: 60"

  @R80 @R81 @R82 @R87
  Scenario: A stopped consumer does not hold up the other, on AWS
    When fan-out runs with "--target aws --updates 20 --rate 0 --stopped-for 0 --keep"
    Then the exit code is 0
    And the output holds "while history was stopped: dashboard stored 20, history stored 0, history queue holds 20"
    And the report of "dashboard consumer" and of "history consumer" each say "updates stored: 20"
    And each consumer's mapping takes batches of 10, reports failed messages and runs at most 2 copies
    And the mapping of "recipe-ana-history" was created stopped and started after the updates were published

  @R83
  Scenario: Only the topic may send to a consumer's queue
    When fan-out runs with "--target aws --updates 10 --rate 0 --stopped-for 0 --keep"
    Then the policy of the queues "recipe-ana-dashboard, recipe-ana-history" lets only "sns.amazonaws.com" send, and only from the topic "recipe-ana-updates-topic"
    And both queues are subscribed to the topic with raw message delivery

  @R84
  Scenario: A message that keeps failing moves to the dead-letter queue
    When fan-out runs with "--target aws --updates 10 --rate 0 --stopped-for 0 --keep"
    Then the queues "recipe-ana-dashboard, recipe-ana-history" move a message to their "-dlq" queue after 5 receives
    And their visibility timeout is 180 seconds, 6 times the function's timeout of 30 seconds

  @R88 @R46
  Scenario: A dead-letter queue keeps a message longer than the queue it came from
    When fan-out runs with "--target aws --updates 10 --rate 0 --stopped-for 0 --keep"
    Then the log groups "/aws/lambda/recipe-ana-dashboard, /aws/lambda/recipe-ana-history" were made before their functions and keep their lines 7 days
    And the queues "recipe-ana-dashboard-dlq, recipe-ana-history-dlq" keep their messages 14 days
    And the queues "recipe-ana-dashboard, recipe-ana-history" keep their messages 4 days

  @R85
  Scenario: A message that is delivered twice is stored once
    Given the table "recipe-ana-dashboard" exists and the fan-out function is told its name
    And 5 queue messages, each one update
    When the fan-out function receives them at 1800000001 seconds, and then again at 1800000050 seconds
    Then the table "recipe-ana-dashboard" holds 5 updates, each with arrival_ms 1800000001000

  @R86
  Scenario: A message that cannot be stored is reported, and the others are stored
    Given the table "recipe-ana-dashboard" exists and the fan-out function is told its name
    And 5 queue messages, each one update
    And message 3 is not JSON
    When the fan-out function receives them at 1800000001 seconds
    Then the fan-out function reports message 3 as failed
    And the table "recipe-ana-dashboard" holds 4 updates
