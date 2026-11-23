# Nightly report from an export, not from the live table

Extension of the [dynamodb recipe](../README.md). Managers want trips per pickup zone and the average fare, every
night. Athena queries a copy of the trip store in S3 for the report. The app depends on the live table.

## What it changes in main/

- Turns on point-in-time recovery (PITR) for `$PREFIX-trips`. With PITR, DynamoDB keeps a continuous backup of the
  table, and exports are taken from that backup.
- Writes a full export to `s3://$BUCKET/$PREFIX/exports/` and an Athena table over it. A full export writes the whole
  table, as of one moment, to S3 as files.
- Run `main/run.sh` first with `KEEP=1`; the extension does not change it. Set `BUCKET` to an S3 bucket you own in
  `ca-central-1`. DynamoDB can export to a bucket in another Region; keep it in this one, so the data stays in the
  Region of the course
  (whether a bucket in another Region adds a transfer charge was not checked).

## Steps

1. Run the report with a `Scan` that reads every trip through the table.
2. Turn on PITR.
3. Start a full export to S3.
4. Wait for the export. The script checks its status every 30 seconds. An export that ends with any status but `COMPLETED` stops the run with
   a `FAILED` line.
5. List the files in S3: a manifest and gzipped JSON data files.
6. Create an Athena table over the data files.
7. Query the copy for trips per pickup zone.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| report source (R60, R61) | an export to S3, not a Scan | an export does not use the table's read capacity, so the app's reads are not slowed. The export leaves files SQL can group, join and keep as history | operational store feeds the lake (L12–L13; [exports do not consume read capacity and need PITR](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/S3DataExport.HowItWorks.html)) |
| freshness | one full export a night | the copy is as fresh as the moment of the export. Changes after it reach the report with the next export | L13 (data latency; export) |
| price, at 1 GB | the export is not cheaper per report | a Scan of 1 GB is 131,072 read units, about 0.018 USD; a full export is 0.11 USD | AWS bulk price list for ca-central-1: export 0.11 USD per GB, PITR 0.22 USD per GB-month, reads 0.1375 USD per million units |
| query that fails (R64) | the run stops with a `FAILED` line that names the state and the clean-up command | a later step would read nothing and print an empty report | Course choice |
| names (R62, R63) | `BUCKET` does not have a default. The Athena database is `<PREFIX>_lake` with `-` written as `_` | a default bucket name would belong to someone else. A database name with a hyphen has to be written in backticks in every Athena statement | course constraint (names start with `$PREFIX`); [Athena, DROP TABLE](https://docs.aws.amazon.com/athena/latest/ug/drop-table.html), the example with special characters |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/nightly_report.feature`.

- **R60** (complex) Where `ext-nightly-report` is used, when the extension runs, the recipe shall turn on
  point-in-time recovery for `$PREFIX-trips` before it starts the export.
- **R61** (optional) Where `ext-nightly-report` is used, the recipe shall write the export under
  `s3://$BUCKET/$PREFIX/exports/` and the Athena results under `s3://$BUCKET/$PREFIX/athena-results/`.
- **R62** (complex) Where `ext-nightly-report` is used, if `BUCKET` is not set, then the recipe shall exit with a
  message that names `BUCKET`, before any call to AWS.
- **R63** (optional) Where `ext-nightly-report` is used, the recipe shall name its Athena database `$PREFIX_lake`,
  with every `-` of the prefix written as `_`.
- **R64** (complex) Where `ext-nightly-report` is used, if an Athena query does not succeed, then the recipe shall
  print the state the query ended in and exit with code 1.
- **R65** (complex) Where `ext-nightly-report` is used, if the export ends with a status other than `COMPLETED`,
  then the recipe shall print `FAILED` with the status and exit with code 1 before it asks Athena anything.

## Run it

Run by us on 2026-10-21 (`../output/ext-nightly-report.txt`). From your sandbox terminal, sign in and check that the account is the one you expect. The recipe README explains
what a sandbox terminal is:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

From your sandbox terminal in `dynamodb`, use your own name in place of `ana` and a bucket you own in place of
`my-report-bucket`:

```bash
export PREFIX=recipe-ana AWS_REGION=ca-central-1 BUCKET=my-report-bucket
KEEP=1 bash main/run.sh                       # first, once: the trips table stays
DRY=1 bash ext-nightly-report/run.sh          # prints every command and query, runs nothing
bash ext-nightly-report/run.sh                # the export takes a few minutes; then cleans up the whole recipe
```

Clean-up: the run ends with it, and because `BUCKET` is set it also deletes the export and the Athena results. After
a run with `KEEP=1`, or one that stopped with a `FAILED` line, run `bash clean_up.sh` (it lists what it would delete), then `bash clean_up.sh --yes` in the same terminal with the
same `PREFIX` and `BUCKET`. Cost: see below.

## What you should see

Captured: [../output/ext-nightly-report.txt](../output/ext-nightly-report.txt). PITR `"ENABLED"`; the export
`IN_PROGRESS`, then `COMPLETED` (94 s of waiting in that run; AWS promises no completion time for an
export); four `data/*.json.gz`
files and two manifests. The report has five zones with counts and average fares. Data scanned was 956 bytes. That run followed
`ext-riders-and-drivers`, so the Scan read 52 items, not 50.

The columns follow DynamoDB's types. `Item.pickup_zone.S` is a string. `Item.fare_quote.N` is a number stored as text, so
the query casts it (`CAST(Item.fare_quote.N AS double)`). An attribute missing from an item reads as `NULL`.

## Cost

For this small table every part is well under a cent (the export billed 0 bytes). PITR bills 0.22 USD per GB-month
while it is on; `bash clean_up.sh` (it lists what it would delete), then `bash clean_up.sh --yes` deletes the table, which ends it. The Athena query's Data scanned was 956 bytes. Athena bills
5.50 USD per TB of Data scanned in ca-central-1 (AWS bulk price list).
