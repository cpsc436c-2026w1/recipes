Feature: Replaying a data set at a chosen speed
  The replayer. Offline the cloud services are moto's stand-ins, and the clock is a test clock that moves only when
  the replayer waits, so an hour at speed 60 takes no time; the 1 s rule is checked on the times sent.jsonl holds.

  @R60
  Rule: Each delivery is sent at its offset divided by the speed, under the run id, and its send time is written down

    Example: Three deliveries at speed 10
      Given these deliveries:
        | delivery_id | event_type     | event_id  | trip_id | pickup_zone_id | event_time               | received_time            |
        | dl-000001   | trip_requested | ev-000001 | t-0001  | 161            | 2026-11-23T08:59:59.000Z | 2026-11-23T09:00:00.000Z |
        | dl-000002   | trip_requested | ev-000002 | t-0002  | 48             | 2026-11-23T09:00:29.000Z | 2026-11-23T09:00:30.000Z |
        | dl-000003   | trip_requested | ev-000003 | t-0003  | 230            | 2026-11-23T09:01:39.000Z | 2026-11-23T09:01:40.000Z |
      When the replayer sends them at speed 10 with run id "r1" to "local"
      Then sent.jsonl has the 3 deliveries, sent 0 s, 3 s and 10 s after the start, each within 1 s
      And the handler received trip ids "r1-t-0001", "r1-t-0002" and "r1-t-0003"

    Example: The rush hour at speed 60 into Kinesis
      Given the simulated data set SIM01 "rush-hour"
      When the replayer sends it at speed 60 with run id "r2" to "kinesis"
      Then 2515 records reached the stream, in PutRecords calls of at most 500
      And the last delivery was sent (its received_time minus the first received_time) / 60 after the start, within 1 s

    Example: The rush hour into the bucket, one file a minute
      Given the simulated data set SIM01 "rush-hour"
      When the replayer sends it at speed 0 with run id "r3" to "s3" with slots of 1 minute
      Then each object under raw/trip-events/dt=2026-11-23/ is named by its slot start and "r3"
      And the objects hold 2515 lines together

    Example: Two replays of the same data set do not share ids
      Given the simulated data set SIM01 "rush-hour" was replayed with run id "r4" to "local"
      When it is replayed again with run id "r5" to "local"
      Then the handler received 5030 deliveries with 1674 distinct trip ids
