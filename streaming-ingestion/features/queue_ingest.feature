@JS01 @prefix_recipe
Feature: Every completed trip stored once, and a broken message kept
  Covers JS01 (operations analyst) of ext-queue-ingest. Offline, moto stands in for SQS, DynamoDB and S3, a stub
  for the message move task (moto does not have it), and run.py's drain() for Lambda's poller: it calls the
  consumer with the batches the queue gives; examples tagged @live run in AWS (LIVE=1). A table holds the only messages of its
  example; its columns are fields of trip_completed in the ext-queue-ingest README, Data, and each row is sent as a
  delivery line: event_type trip_completed, delivery_id "dl-" and the row's event_id, received_time the send time. A simulated data set is named by
  its id; the ext-queue-ingest README describes it (sim/stand_in.py writes it).

  @R90
  Rule: Each completed trip in the queue is stored with its fare and the time it was stored

    Example: One completion
      Given these messages are sent to queue recipe-trip-completed at 2026-11-23T09:20:00Z:
        | event_id | trip_id | driver_id | dropoff_zone_id | trip_miles | trip_time_s | fare  | driver_pay | event_time           |
        | e-9001   | t-0001  | d-0017    | 230             | 2.41       | 812         | 18.40 | 13.05      | 2026-11-23T09:19:58Z |
      When the consumer has processed the queue
      Then table recipe-trips-completed has 1 item, for trip t-0001, with fare 18.40 and a stored_time

    @live
    Example: The data latency of the rush hour
      Given the simulated data set SIM01 "rush-hour" has been sent to queue recipe-trip-completed in AWS
      When the consumer has processed the queue
      Then latency.py joins the 837 items to sent.jsonl by delivery_id
      And it prints 837 latencies and a p95 under 90 s
      And it prints the batch sizes and the 30-day cost they imply

  @R91 @RF020
  Rule: A completion delivered twice is stored once

    Example: The same trip in two messages
      Given these messages are sent to queue recipe-trip-completed:
        | event_id | trip_id | driver_id | dropoff_zone_id | trip_miles | trip_time_s | fare  | driver_pay | event_time           |
        | e-9002   | t-0002  | d-0003    | 161             | 1.10       | 420         | 11.20 | 8.10       | 2026-11-23T09:21:10Z |
        | e-9002   | t-0002  | d-0003    | 161             | 1.10       | 420         | 11.20 | 8.10       | 2026-11-23T09:21:10Z |
      When the consumer has processed the queue
      Then table recipe-trips-completed has 1 item, for trip t-0002
      And the consumer counted 1 stored and 1 repeat

    Example: An hour with 4 completions sent twice
      Given the simulated data set SIM03 "completions-twice" has been sent to queue recipe-trip-completed
      When the consumer has processed the queue
      Then table recipe-trips-completed has 837 items
      And the consumer counted 837 stored, 4 repeats and 1678 other

  @R92
  Rule: A message that cannot be stored is reported alone; the rest of its batch is stored

    Example: One refused write in a batch of three
      Given the table refuses the write of trip t-0412
      And these messages arrive in one batch:
        | event_id | trip_id | driver_id | dropoff_zone_id | trip_miles | trip_time_s | fare  | driver_pay | event_time           |
        | e-9411   | t-0411  | d-0021    | 48              | 0.90       | 360         | 9.80  | 7.00       | 2026-11-23T09:30:01Z |
        | e-9412   | t-0412  | d-0008    | 48              | 3.20       | 1020        | 24.60 | 17.90      | 2026-11-23T09:30:05Z |
        | e-9413   | t-0413  | d-0030    | 230             | 1.75       | 610         | 14.10 | 10.20      | 2026-11-23T09:30:09Z |
      When the consumer receives the batch
      Then the consumer reports only the message of trip t-0412 as failed
      And table recipe-trips-completed has items for trips t-0411 and t-0413

  @R93
  Rule: After 5 receives without being stored, a message moves to the dead-letter queue

    Example: A write refused every time
      Given the table refuses the write of trip t-0412 every time
      And the message of trip t-0412 is in queue recipe-trip-completed
      When the consumer has received the message 5 times and failed it each time
      And 240 s have passed since the 5th receive and the consumer polls again
      Then within 20 s queue recipe-trip-completed-dlq holds the message of trip t-0412
      And queue recipe-trip-completed is empty
      And the queues "recipe-trip-completed-dlq" keep their messages 14 days

  @R94
  Rule: replay_dlq.py moves the dead-letter queue back to the source queue and prints how many it moved

    Example: Three messages moved by a move task
      Given queue recipe-trip-completed-dlq holds 3 messages
      When replay_dlq.py runs
      Then one message move task was started with a rate of 10 messages a second
      And queue recipe-trip-completed-dlq is empty
      And the output holds "moved 3 messages"

    Example: The move task refused
      Given queue recipe-trip-completed-dlq holds 3 messages
      And StartMessageMoveTask is refused with AccessDenied
      When replay_dlq.py runs
      Then each of the 3 messages was sent to recipe-trip-completed before it was deleted from recipe-trip-completed-dlq
      And queue recipe-trip-completed holds 3 messages
      And the output holds "moved 3 messages by copy"

    Example: A send that fails leaves the message in the dead-letter queue
      Given queue recipe-trip-completed-dlq holds 3 messages
      And StartMessageMoveTask is refused with AccessDenied
      And the send of the second message fails
      When replay_dlq.py runs
      Then queue recipe-trip-completed-dlq holds 1 message
      And the output holds "moved 2 messages by copy, 1 failed"

  @R95
  Rule: At most 2 copies of the consumer run, with a batching window of 30 to 60 s and a visibility timeout of six times the function's timeout plus the window

    Example: The event source mapping as created
      When run.py creates the queue and the consumer
      Then the consumer's mapping takes batches of 100, reports failed messages and runs at most 2 copies
      And the mapping's batching window is 60 s
      And the queue's visibility timeout is 240 seconds

    Example: A window of 30 s
      Given BATCH_WINDOW_S is 30
      When run.py creates the queue and the consumer
      Then the mapping's batching window is 30 s
      And the queue's visibility timeout is 210 seconds

    Example: No batching window is refused
      Given BATCH_WINDOW_S is 0
      When run.py creates the queue and the consumer
      Then run.py stops with "BATCH_WINDOW_S must be 30 to 60"
      And no queue or mapping was created

  @R96
  Rule: With the S3 sink, each batch becomes one new JSON Lines object, and no object is written over

    Example: One batch, delivered twice
      Given the consumer's sink is S3
      And these messages arrive in one batch on 2026-11-23:
        | event_id | trip_id | driver_id | dropoff_zone_id | trip_miles | trip_time_s | fare  | driver_pay | event_time           |
        | e-9411   | t-0411  | d-0021    | 48              | 0.90       | 360         | 9.80  | 7.00       | 2026-11-23T09:30:01Z |
        | e-9413   | t-0413  | d-0030    | 230             | 1.75       | 610         | 14.10 | 10.20      | 2026-11-23T09:30:09Z |
      When the consumer receives the batch twice
      Then raw/trip-events/dt=2026-11-23/ holds 2 objects with different keys, of 2 lines each
      And each line is the delivery line as it was sent
      And every PutObject call carried If-None-Match "*"

    Example: The mapping of the S3 sink
      Given the consumer's sink is S3
      When run.py creates the queue and the consumer
      Then the mapping takes batches of 100 with a batching window of 60 s
      And the queue's visibility timeout is 240 seconds

    Example: A failed write fails the batch
      Given the consumer's sink is S3
      And PutObject fails
      When the consumer receives a batch of 3 messages
      Then the consumer reports all 3 messages as failed

  @RF010
  Rule: A message that cannot be processed is kept, does not block the others, and is stored after the replay

    Example: One refused write in a simulated hour
      Given the simulated data set SIM02 "refused-write" has been sent to queue recipe-trip-completed
      When the consumer has processed the queue
      Then table recipe-trips-completed has 836 items
      And queue recipe-trip-completed-dlq holds 1 message, for trip t-0412
      When the scenario clears the fault and replay_dlq.py runs
      And the consumer has processed the queue
      Then table recipe-trips-completed has 837 items
      And queue recipe-trip-completed-dlq is empty

  @R97
  Rule: With the DynamoDB sink, a request or an accept stores nothing and is counted as other

    Example: The rush hour's three kinds
      Given the simulated data set SIM01 "rush-hour" has been sent to queue recipe-trip-completed
      When the consumer has processed the queue
      Then table recipe-trips-completed has 837 items
      And the consumer counted 837 stored, 0 repeats and 1678 other
