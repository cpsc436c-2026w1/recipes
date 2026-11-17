# Logs and metrics Lambda already keeps

Use this extension of the [lambda-dynamodb recipe](../README.md) to find the function's logs, what each call was billed,
and how many calls the function had in the last hour and how long they took.

## What it changes in main/

Nothing. It reads what the calls of `main/run.sh` step 8 left: run `KEEP=1 bash main/run.sh` first.

## Steps

1. The script prints the function's log group, `/aws/lambda/$PREFIX-api`, and how many bytes it stores (CloudWatch Logs).
2. The script prints the `REPORT` line Lambda writes after every call: duration, billed duration, memory size and memory used. The
   handler's own JSON line per request is in the same log group; to read those, change the filter pattern in
   `run.sh` to `'{ $.outcome = "refused" }'`.
3. The script prints calls in the last hour (`Invocations`, sum) and their average and longest `Duration`
   (CloudWatch metrics, namespace `AWS/Lambda`).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| what to read first (R90, R91, R92, R93) | what AWS already records for every function | the `REPORT` lines and the `AWS/Lambda` metrics do not need any code, and there is no additional charge for these metrics | observability of cost and errors (L16; [Lambda metrics](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics.html)) |
| period (R92) | one hour, one data point | enough to see the calls of one run; an alarm on these metrics is the next step | L16 |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R90** (optional) Where `ext-metrics` is used, the recipe shall print the function's log group and the bytes it
  stores.
- **R91** (optional) Where `ext-metrics` is used, the recipe shall print at most 8 `REPORT` lines of the function's
  log group.
- **R92** (optional) Where `ext-metrics` is used, the recipe shall print the sum of `Invocations` and the average
  and the longest `Duration` of the function for the last hour.
- **R93** (optional) Where `ext-metrics` is used, the recipe shall only read: it creates, changes and deletes
  nothing.

## Run it

Run by us on 2026-11-14 in a course sandbox (`../output/ext-metrics.txt`), about two minutes after a main run and
one `ext-redeploy` run. The log group contained 7 `REPORT` lines, and `Invocations` was 7. The average duration was 144.6 ms, and the longest duration was 316.02 ms.
Sign in and check that the account is the one you expect. The recipe README explains what a sandbox terminal is.

In a sandbox terminal:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account whose function you want to read
```

In the sandbox terminal, in `lambda-dynamodb`, with the `PREFIX` of your `KEEP=1 bash main/run.sh`:

```bash
export PREFIX=recipe-ana AWS_REGION=ca-central-1
bash ext-metrics/run.sh
```

This extension has nothing to remove. The log group goes with `bash clean_up.sh`. See the cost below.

## What you should see

The script prints the log group and the bytes it stores, along with one `REPORT` line per call. The first line
has an `Init Duration`, as does the first after each redeploy. The script prints a sum equal to the number of calls
in the hour and the average and longest duration.
In the captured run, the average duration was well below the longest, which was the call that started a new environment. `stored_bytes` can still be 0 minutes after the calls: the
captured run printed 0 with all seven `REPORT` lines already in the log group. Lambda sends its metrics to CloudWatch in one-minute intervals, so the calls of the last minute may be
missing.

## Cost

Reading logs and metrics this way is well under one cent. Log storage bills 0.033 USD per GB-month in ca-central-1
while the log group exists (AWS bulk price list); `bash clean_up.sh` deletes it.
