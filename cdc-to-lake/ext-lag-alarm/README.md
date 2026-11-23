# Extension: lag from change to queryable, with an alarm

**What it adds.** The probe script writes a probe trip and measures the whole path from the DynamoDB write to a row Athena returns. It puts the
number in CloudWatch (`CdcToLake/ChangeToQueryableSeconds`), and an alarm goes off when the number passes
`LAG_LIMIT_SECONDS` (300 by default). A second alarm watches Lambda's `IteratorAge`, the age of the newest stream
record when the function read it. The probe also writes `s3://<bucket>/freshness/trip_changes.json` with the newest
`change_time` in the lake, the "data last updated" value a dashboard shows beside its numbers.

**What changes in the main scenario.** After step 6, the probe writes and removes one probe item. Its two rows stay in
the change log, and `trips_current` leaves them out. The extension creates two alarms and one S3 object. `main/` is unchanged.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| What to measure (R60) | Change to queryable, with a probe item | Users care when a change shows in a query, not when Lambda saw it. Only a probe crosses every part | L13 (freshness); Data Analytics Lens [BP 1.2](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-1.2---monitor-operational-metrics-of-data-processing-jobs-and-the-availability-of-source-data..html) (monitor job metrics and the availability of source data) |
| Second signal (R62) | `IteratorAge` from AWS/Lambda | Lambda publishes it for every batch; it shows a stuck consumer between probes | Lambda docs, stream metrics |
| Limit (R61) | 300 s (course default: five times the 60 s buffer) | Below the buffer interval the alarm would always be on. A higher threshold allows more lag before the alarm detects a stuck pipeline | Course choice; set it from what your dashboard promises |
| Missing probe in a period (R63) | `INSUFFICIENT_DATA`, not `OK` | Without a probe, the lag is unknown, so the alarm cannot confirm that the lag is fine | CloudWatch docs, [alarms and missing data](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html) |
| Freshness for the dashboard (R64) | A small JSON object in S3 | The dashboard reads one object instead of running an Athena query to show a date | Course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R60** (complex) Where `ext-lag-alarm` is used, when `lag.py --target aws` runs, the recipe shall write a probe
  trip, wait until Athena returns its row, put the seconds from the change to that moment into the metric
  `CdcToLake/ChangeToQueryableSeconds`, and remove the probe.
- **R61** (optional) Where `ext-lag-alarm` is used, the recipe shall set the alarm `$PREFIX-cdc-lag` to go off when
  a probe's lag is more than 300 seconds, unless `LAG_LIMIT_SECONDS` names another number.
- **R62** (optional) Where `ext-lag-alarm` is used, the recipe shall set the alarm `$PREFIX-cdc-iterator-age` to go
  off when the function's `IteratorAge` is more than 60,000 milliseconds in 3 of 5 minutes.
- **R63** (optional) Where `ext-lag-alarm` is used, the recipe shall leave the lag alarm without a state of `OK`
  for a period that has no probe.
- **R64** (complex) Where `ext-lag-alarm` is used, when a probe has been measured, the recipe shall write the newest
  change time in the lake and the probe's lag to `freshness/trip_changes.json` in the lake's bucket.

## Run it

Offline, on your laptop, in `cdc-to-lake` (a simulated clock, moto for CloudWatch):

```bash
uv run python ext-lag-alarm/lag.py
```

In a sandbox terminal (the recipe README says what that is), after `python -m main.run --target aws`. Run by us on
2026-11-13 at 04:33 UTC (`../output/ext-lag-alarm.txt`). Sign in and check the account:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account of your pipeline
```

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-lag-alarm/lag.py --target aws
```

The probe runs when you run it. To keep the alarm meaningful, run it on a schedule of your own (for example every
15 minutes from a team runner) and say so in your README. Clean-up: `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` in `cdc-to-lake`
removes both alarms and the freshness object. Cost: see below.

**What you should see** (`output/lag-local.txt`): a probe through a functioning pipeline at about the buffer interval (62 s: 2 s to the
function, 60 s buffer) with the alarm `OK`, and a probe through a function that failed for 400 s at 462 s with the
alarm `ALARM`. The times come from a simulated clock, not a measurement. On AWS (`output/ext-lag-alarm.txt`,
2026-11-13 04:33 UTC, 60 s buffer) the probe measured 91 s from change to queryable, under the 300 s limit.

**Cost.** Two standard alarms at 0.10 USD each per month and one custom metric at 0.30 USD per month in
ca-central-1: 0.50 USD a month while they exist (AWS bulk price list).
Each of the probe's Athena queries has a few kilobytes of Data scanned.
