# Experiment: replay-speed

A question for your own system, with a method: how replay speed changes data latency. Sections 1 to 6 are written
before the run; 7 and 8 after it.

## 1. Question

For the deployed counting path, how does the data latency at p95 change when SIM01 is replayed at 1, 10 and 50 times
its speed?

## 2. Design alternatives

| Alternative | What it is | Why it might win |
|---|---|---|
| speed 1 | SIM01's deliveries in their own time: 2 h 17 min (its completions run past the hour; 137 s at speed 60, measured) | the rate the design was written for |
| speed 10 | about 14 minutes | ten times the rate, still under one shard's 1,000 records a second |
| speed 50 | about 2 minutes 45 seconds | shows where the consumer's batching and the lease's 10 Lambda copies start to add delay |

## 3. Overall evaluation criterion and guardrails

- **Overall evaluation criterion (OEC):** data latency at p95 over SIM01's 2,511 events, in seconds, lower is better.
  For each event: the first time the deployed table shows that event's effect (a request: its trip has a row with its
  zone; an accept: the trip's state is accepted or later; a completion: the trip is completed with its fare), minus
  the `sent_time` of the event's first delivery in `sent.jsonl`, joined on `delivery_id` (with the run id in front).
  The read step polls the table every 5 s; an event not shown by the end of the run counts as over every other. The
  p95 is by the nearest rank, the 2,386th smallest.
- **Guardrail metrics:** 0 deliveries lost (the table equals the answer from `_truth/`); Lambda `Throttles` 0; the run's cost under
  0.10 USD.
- **Requirement it informs:** R60 of the README.

## 4. Experiment setup

- **Same for all alternatives:** SIM01 with seed 436, the deployed stream of 1 provisioned shard, the same consumer
  and table; a new run id for each replay, so no replay is taken for repeats of another.
- **Test cases:** one workload, SIM01's 2,515 deliveries, at each speed.

## 5. Prediction

Written by the team before the run, with its reasoning (L13: data latency and percentiles; L14: the batching window of
an event source mapping).

## 6. Method

- **Command:** `uv run python sim/replay.py SIM01 --to kinesis --speed <k> --run-id <date>-SIM01-k<k>`, then
  `uv run python experiments/latency.py evidence/<run-id>/` (your read step writes `seen.jsonl`: one line `{"event_id", "seen_time"}` per event)
- **Repeats:** 5 replays per speed, each with its own run id. Speed 1 takes 2 h 17 min each: run it from a laptop
  terminal after a fresh sign-in, not from CloudShell (it ends after 20 to 30 minutes without keyboard input).
- **Evidence:** `evidence/<run-id>/` for each replay (command, commit SHA, `sent.jsonl`, the first-seen times, the
  script that rebuilds the table)

## 7. Result

After the run: median and interquartile range of the p95 over the 5 replays of each speed.

| Alternative | Runs | Median | Spread | Unit |
|---|---|---|---|---|
| speed 1 | | | | s |
| speed 10 | | | | s |
| speed 50 | | | | s |

## 8. What you conclude

After the run.
