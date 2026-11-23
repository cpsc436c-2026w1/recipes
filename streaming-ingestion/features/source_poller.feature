@JS01 @prefix_recipe
Feature: Every slot of the partner's feed, once, and the missing ones shown
  Covers JS01 (operations analyst) of ext-source-poller. Offline, moto stands in for S3, DynamoDB and Kinesis, and
  the partner's manifest at each run lists the files published by then. A table holds the only
  manifest entries of its example. A slot is named by its start in UTC and ends 15 minutes later. "The page lists day D at T" calls
  GET /api/slots?day=D (dashboard-page's route, which imports ext-source-poller/slots.py) with the clock at T. Data
  sets are described in the ext-source-poller README, Data (sim/stand_in.py and feed.py write them).

  @R100
  Rule: Each run loads every listed slot that ended in the last 25 hours, is not loaded yet and was published within 24 hours of its end

    Example: Two new slots in the manifest
      Given the manifest lists these slots:
        | slot              | file                         | published_time       |
        | 2026-11-22T08:00Z | trips-2026-11-22T0800Z.jsonl | 2026-11-22T08:17:00Z |
        | 2026-11-22T08:15Z | trips-2026-11-22T0815Z.jsonl | 2026-11-22T08:32:00Z |
      And slot 2026-11-22T08:00Z is loaded
      When the poller runs at 2026-11-22T08:45:00Z
      Then the poller loaded slot 2026-11-22T08:15Z only

    Example: A file published just inside the 24 hours
      Given the manifest lists these slots:
        | slot              | file                         | published_time       |
        | 2026-11-22T08:00Z | trips-2026-11-22T0800Z.jsonl | 2026-11-23T08:05:00Z |
      When the poller runs at 2026-11-23T08:15:00Z
      Then slot 2026-11-22T08:00Z is loaded and marked late

    Example: A file published after the 24 hours
      Given the manifest lists these slots:
        | slot              | file                         | published_time       |
        | 2026-11-22T08:00Z | trips-2026-11-22T0800Z.jsonl | 2026-11-23T08:25:00Z |
      When the poller runs at 2026-11-23T08:30:00Z
      Then the poller loaded no slot
      And slot 2026-11-22T08:00Z does not have an item

    Example: A run just after midnight reads the items of three dates
      Given slot 2026-11-21T23:15Z is loaded, and its item is under slot_day 2026-11-21
      And the manifest lists slot 2026-11-21T23:15Z
      When the poller runs at 2026-11-23T00:10:00Z
      Then the poller queried slot_day 2026-11-21, 2026-11-22 and 2026-11-23
      And the poller loaded no slot

    Example: A whole day of the feed
      Given the simulated data set SIM01 "feed-day" has been published
      When the poller has run every 15 minutes from 2026-11-22T00:15:00Z to 2026-11-23T02:00:00Z
      Then 95 slots are loaded, 1 of them late
      And table recipe-updates holds each trip of the 95 published files once

    @live
    Example: The data latency of a day
      Given the simulated data set SIM01 "feed-day" has been published in AWS
      When the poller has run every 15 minutes from 2026-11-22T00:15:00Z to 2026-11-23T02:00:00Z
      Then latency.py prints 95 latencies and a p95 under 20 minutes

  @R101 @RF020
  Rule: A slot is loaded only by the run whose conditional claim succeeds

    Example: Two runs at the same moment
      Given the simulated data set SIM02 "overlapping-runs" has been published
      When two poller runs start at 2026-11-22T08:45:00Z
      Then each of slots 2026-11-22T08:00Z and 2026-11-22T08:15Z was sent by one run only
      And the stream received each trip of the two files once

  @R102
  Rule: A slot's records go to the stream in calls of at most 500, and only refused records are sent again

    Example: A slot of 1,203 trips with 7 refused
      Given slot 2026-11-22T17:00Z has a file of 1203 trips
      And the stream refuses the first 7 records of its first 1 calls
      When the poller loads the slot
      Then the PutRecords calls carry "500, 500, 203, 7" records
      And slot 2026-11-22T17:00Z is loaded with 1203 records

  @R103
  Rule: A file published, or first listed, more than 30 min after its slot ended is loaded at the next run and marked late

    Example: The 10:30 slot published 2 hours after it ended
      Given the simulated data set SIM01 "feed-day" has been published up to 2026-11-22T12:50:00Z
      When the poller runs at 2026-11-22T13:00:00Z
      Then slot 2026-11-22T10:30Z is loaded and marked late

    Example: A feed without publication times
      Given the manifest lists these slots:
        | slot              | file                         | published_time |
        | 2026-11-22T08:00Z | trips-2026-11-22T0800Z.jsonl |                |
      When the poller runs at 2026-11-22T08:30:00Z
      Then slot 2026-11-22T08:00Z is loaded with first_seen_time 2026-11-22T08:30:00Z and not marked late

  @R104 @RF010
  Rule: The page shows each slot of a day with its state, and missing only more than 30 min after the slot ended

    Example: The page at 10:00 and at 10:01
      Given the manifest lists these slots:
        | slot              | file                         | published_time       |
        | 2026-11-22T09:00Z | trips-2026-11-22T0900Z.jsonl | 2026-11-22T09:17:00Z |
        | 2026-11-22T09:30Z | trips-2026-11-22T0930Z.jsonl | 2026-11-22T09:47:00Z |
      And the poller ran at 2026-11-22T09:45:00Z and at 2026-11-22T10:00:00Z
      When the page lists day 2026-11-22 at 2026-11-22T10:00:00Z
      Then slot 2026-11-22T09:00Z is shown as loaded
      And slot 2026-11-22T09:15Z is shown as not due
      And slot 2026-11-22T09:30Z is shown as loaded
      And slot 2026-11-22T09:45Z is shown as not due
      When the page lists day 2026-11-22 at 2026-11-22T10:01:00Z
      Then slot 2026-11-22T09:15Z is shown as missing

    Example: The response of the slot list
      Given the simulated data set SIM01 "feed-day" has been published
      And the poller has run every 15 minutes up to 2026-11-23T00:45:00Z
      When the page lists day 2026-11-22 at 2026-11-23T00:46:00Z
      Then the response has 96 slots in slot order, each with a slot_state
      And GET /api/slots?day=yesterday gets 400

    Example: A slot being loaded
      Given the run at 2026-11-22T08:30:00Z claimed slot 2026-11-22T08:00Z and is still sending
      When the page lists day 2026-11-22 at 2026-11-22T08:31:00Z
      Then slot 2026-11-22T08:00Z is shown as loading

    Example: A day with one slot never published
      Given the simulated data set SIM01 "feed-day" has been published
      And the poller has run every 15 minutes up to 2026-11-23T00:45:00Z
      When the page lists day 2026-11-22 at 2026-11-23T00:46:00Z
      Then it shows 95 slots loaded, 1 of them late, and 1 missing, slot 2026-11-22T09:15Z

  @R105
  Rule: A claim left loading for more than 15 min is taken over and the slot sent again

    Example: A run that stopped after its claim
      Given the simulated data set SIM03 "stopped-run" has been published
      And the run at 2026-11-22T08:30:00Z claimed slot 2026-11-22T08:00Z and stopped
      When the poller runs at 2026-11-22T08:45:00Z
      Then slot 2026-11-22T08:00Z is still loading
      When the poller runs at 2026-11-22T09:00:00Z
      Then slot 2026-11-22T08:00Z is loaded

  @R106
  Rule: A slot whose load fails is marked failed with its error and claimed again at the next run

    Example: A file cut off on the first read
      Given the simulated data set SIM04 "unreadable-file" has been published
      When the poller runs at 2026-11-22T08:30:00Z
      Then slot 2026-11-22T08:00Z is failed, with an error that holds "line"
      When the poller runs at 2026-11-22T08:45:00Z
      Then slot 2026-11-22T08:00Z is loaded
