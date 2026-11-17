# Change data capture: DynamoDB changes into an Iceberg table that Athena queries

## What you build

Every insert, update and delete on a DynamoDB table travels through the table's change stream into an Iceberg table
in S3, and Athena queries it as a change log and as the latest state of each item. Each row carries the time the
item changed and the time the Lambda function received the change, so you can measure change-to-arrival time as part of lake freshness.

## Use it when

- Your project has the lakehouse or operational shape and you chose the change data capture extension (2 points);
  the recipe's extensions serve lag monitoring, table maintenance and rollback (1 point each).
- The app writes to DynamoDB and reports or dashboards need the same data within minutes, without scanning the table.
- If a nightly copy is enough, choose a full export: it is simpler and does not need a stream consumer (`dynamodb/ext-nightly-report`).
- You need the items in a search index rather than in the lake: `zero-etl-search`.
- Lectures: L11 (lakehouse tables, Iceberg snapshots), L13 (change streams, freshness).

## Main scenario

1. The app writes trips to the DynamoDB table `$PREFIX-trips`. Its stream records every change with the old and the
   new item (`NEW_AND_OLD_IMAGES`).
2. A Lambda event source mapping connects the stream to the function: it reads the stream in batches and calls `main/handler.py`.
3. The function turns each record into one change row: the key, the operation (`INSERT`, `MODIFY`, `REMOVE`),
   `change_time` from the stream, `arrival_time` when the function received it, and the item's fields.
4. The function sends the rows to an Amazon Data Firehose stream with an Iceberg table as its destination.
5. Firehose buffers the rows for `BUFFER_SECONDS` (60 by default) and then commits them to the Iceberg table
   `trip_changes`. The table is in the Glue Data Catalog and stored in S3.
6. Athena queries the change log and the view `trips_current`, which picks the latest row of each trip. The
   freshness query reports the newest `change_time` ("data last updated") and the longest change-to-arrival time.

Offline, steps 1 and 3 run as written. Moto, a library that stands in for AWS services, supplies DynamoDB and Streams,
and the run uses the same handler. A loop stands in for the event source mapping. `main/lake.py` stands in for
Firehose with a buffer and one commit. A real Iceberg table on disk with a SQLite catalog stands in for the Glue
Data Catalog. DuckDB runs the same SQL as Athena.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| How changes leave the table (R30, R35) | DynamoDB Streams, read by a Lambda event source mapping | The stream holds every change for 24 hours, in order for each item ([DynamoDB Streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html)); the app writes once, to DynamoDB only | L13 (change streams, dual writes); [FDE ch. 5](https://learning.oreilly.com/library/view/fundamentals-of-data/9781098108298/ch05.html), "Change Data Capture", and [DDIA 2e ch. 12](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch12.html), "Databases and Streams" |
| Stream view (R12, R30) | `NEW_AND_OLD_IMAGES` | A `REMOVE` record then still carries the removed item's values, so the lake can say what was deleted | [DynamoDB Streams, enabling a stream](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html) |
| Lake table shape (R21, R22, R27, R33) | Append-only change log: one row per change, never updated in place | History stays queryable, including what a trip looked like at noon. A retried batch adds a duplicate row, not a wrong value. Current state is a view, and the report counts a change once, by its trip and sequence number | L11; Data Analytics Lens [BP 10.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-10.html) (append-only or in-place update); Firehose API reference, [`AppendOnly`](https://docs.aws.amazon.com/firehose/latest/APIReference/API_IcebergDestinationConfiguration.html): with it Firehose raises the stream's throughput limit by itself, and a stream that also updates or deletes rows then delivers out of order |
| From Lambda to S3 (R10, R13, R33) | Firehose Direct PUT with an Iceberg destination | Firehose batches rows into data files and commits them to the table, so there is no writer code to maintain | Firehose docs, [Iceberg tables: considerations](https://docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-considerations.html) |
| Table format (R32) | Iceberg in the Glue Data Catalog, Parquet, zstd | Snapshots give time travel and rollback (ext-rollback-or-schema); Athena reads and maintains it | L11 (lakehouse tables) |
| Partitioning (R32) | `day(change_time)`, a hidden partition | A query over the last day reads only that day's files. Queries filter on `change_time` itself | L11 (partition pruning); Data Analytics Lens [BP 10.4](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-10.html) |
| Catalog (R32) | Glue Data Catalog | Athena, Firehose and PyIceberg find the same table and its current snapshot in one place | Data Analytics Lens [BP 7.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-7.html) (central Data Catalog) |
| Two times per row (R11, R25) | `change_time` from the stream (whole seconds), `arrival_time` in the function | Lag has parts: the stream and Lambda before arrival, then the Firehose buffer and the commit. `arrival_time` minus `change_time` measures the first part; the time a row becomes queryable (the probe in `ext-lag-alarm`) gives the rest | L13 (freshness) |
| Ordering within one second (R11, R24) | `seq`: the stream's sequence number, padded to 40 digits (course default) | `change_time` has whole seconds only; padding makes text order equal number order in Athena and DuckDB. The stream keeps the changes of one item in order | [DynamoDB Streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html); that a sequence number never has more than 40 digits is not on that page |
| Buffer interval (R23, R33) | 60 s (course default, set in `BUFFER_SECONDS`) | The largest part of the lag; shorter means fresher data and more, smaller files (ext-maintenance) | Firehose API reference, [BufferingHints](https://docs.aws.amazon.com/firehose/latest/APIReference/API_BufferingHints.html) (0 to 900 s; a hint, which Firehose may change) |
| Failed records (R14, R15, R35) | Retry within the function (3 attempts, course default), then report the first failed record (`ReportBatchItemFailures`), split the batch on error, give up on a batch after 10 retries (course default; 11 tries in all); what Lambda then gives up on is recorded in the queue `$PREFIX-cdc-failures` (the batch's shard and sequence numbers, not the records) | Without a limit Lambda retries a failed batch until its records leave the stream, so one bad record would hold back the rest of the shard for 24 hours. Records before the failed one are not sent again (those after it may be: the change log keeps a duplicate row, and `trips_current` picks one). A change that is dropped without a trace leaves the lake wrong for good and nobody knows; with the queue, its position in the stream is on record while the stream still holds it (24 hours) | Lambda docs, [Configuring partial batch response with DynamoDB and Lambda](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html); Firehose API reference, [PutRecordBatch](https://docs.aws.amazon.com/firehose/latest/APIReference/API_PutRecordBatch.html): resend only the records that failed; Lambda docs, [on-failure destinations for a DynamoDB stream](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html); Serverless Applications Lens [REL 2, failure management](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/failure-management.html) (Maximum Retry Attempts and Destination on Failure send a failing batch's details to a queue) |
| Start position (R35) | `TRIM_HORIZON` | Changes made before the mapping existed still reach the lake | Lambda docs, event source mapping |
| Roles (R36, R37) | `main/infra.py` `ensure_roles` creates one role per service with only the actions that service needs. A team can instead pass in roles using `LAMBDA_ROLE_ARN`, `FIREHOSE_ROLE_ARN` and `SCHEDULER_ROLE_ARN` | The permissions limit a mistake in one part to one stream, one table and one bucket. A team that already has roles can reuse them | [SEC03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html) grant least privilege access |
| Athena's result files (R57) | in a bucket the recipe created, S3 deletes everything under `athena-results/` 7 days after it was written (course default); a bucket passed in as `BUCKET` keeps its own rules | every query writes a result file and a metadata file there (53 objects after our run of the main scenario and the extensions), nobody reads them after the run, and the two nightly schedules of `ext-maintenance` add to them every night | Data Analytics Lens [BP 13.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-13.html) (remove unused data) |
| Table capacity (R42) | on demand, with a ceiling of 100 read and 100 write request units per second (course default) | nothing to size for a few trips. Every write to the table is also a stream record, a Lambda call and a Firehose row, so a loop that writes without end is billed four times over; the ceiling throttles it at the source. An hour at the ceiling is at most about 0.25 USD of DynamoDB writes | [DynamoDB Well-Architected Lens](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-wal.html#bp-wal-pillars), cost optimization; [maximum throughput for on-demand tables](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode-max-throughput.html) |
| One SQL text (R20, R26) | `main/sql/*.sql` runs in Athena and DuckDB | The offline test checks the same query that runs in Athena | Course choice |
| Run over what an earlier run left (R39, R55) | a table or a Firehose stream that is still being deleted is waited out and created again; a clean-up reports it as already being deleted | a run right after a clean-up would otherwise fail on its first call | [CreateTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_CreateTable.html) names `ResourceInUseException` for a table that exists; that it is also the answer for one in `DELETING` is not on that page |

## Requirements

Each requirement states what the recipe must do in one sentence, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets
after a requirement's number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an extension is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. The requirements of an extension are in its own README, and their numbers go on from here:
[lag alarm](ext-lag-alarm/README.md#requirements) from 60,
[rollback or schema change](ext-rollback-or-schema/README.md#requirements) from 70,
[maintenance](ext-maintenance/README.md#requirements) from 80. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Function** (`main/handler.py`, in Lambda)

- **R10** (event) When the function receives a batch of stream records, the recipe shall send one change row per
  record, in the order of the stream, to the Firehose stream named in `DELIVERY_STREAM`.
- **R11** (ubiquitous) The recipe shall give every change row the trip's key, the operation, the change time and the
  arrival time in microseconds, the stream's sequence number padded to 40 digits, and the item's fields in the
  types of the lake table.
- **R12** (event) When a stream record is a `REMOVE`, the recipe shall put the removed item's last values into its
  row.
- **R13** (ubiquitous) The recipe shall send at most 500 rows in one call to Firehose.
- **R14** (unwanted) If Firehose rejects some rows of a call, then the recipe shall send only those rows again, in
  at most 3 calls in all for one group of rows.
- **R15** (unwanted) If rows are still rejected after the 3rd call, then the recipe shall report the rejected record
  with the lowest sequence number to Lambda as the first failed one.

**Lake**

- **R20** (event) When `python -m main.run` runs, the recipe shall write three trips, change one, remove one, and
  print the latest state of each trip from the change log.
- **R21** (ubiquitous) The recipe shall keep every change as a row of its own in `trip_changes`.
- **R22** (ubiquitous) The recipe shall leave out of `trips_current` every trip whose latest change is a `REMOVE`.
- **R23** (state) While rows wait in the Firehose buffer, the recipe shall show in its report that a query returns
  none of them.
- **R24** (ubiquitous) The recipe shall take as a trip's latest change the one with the latest change time, and
  among changes of the same second the one with the highest sequence number.
- **R25** (event) When a run ends, the recipe shall print the newest change time in the lake and the longest time
  from a change to its arrival at the function.
- **R26** (ubiquitous) The recipe shall run `python -m main.run` without an AWS account or a network connection.
- **R27** (unwanted) If the same change reaches the lake twice, then the recipe shall count it once in its report.
  (`trips_current` shows each trip once in any case, by R22 and R24.)

**Pipeline in the sandbox** (`python -m main.run --target aws`)

- **R30** (event) When a run with `--target aws` starts, the recipe shall create the table `$PREFIX-trips` with a
  stream that carries the new and the old image of every change.
- **R31** (ubiquitous) The recipe shall block public access to the bucket it creates.
- **R32** (event) When a run with `--target aws` starts, the recipe shall create the Iceberg table `trip_changes`,
  partitioned by the day of `change_time`, and the view `trips_current`, in the Glue Data Catalog.
- **R33** (ubiquitous) The recipe shall have Firehose append rows to `trip_changes` after a buffer of 60 seconds
  unless `BUFFER_SECONDS` names another number.
- **R34** (event) When a run with `--target aws` starts, the recipe shall create the function from `main/handler.py`
  alone, with Python 3.12, 256 MB of memory and a timeout of 60 seconds.
- **R35** (ubiquitous) The recipe shall have Lambda read the stream from its oldest record, in batches of at most
  100 records or 5 seconds, and after 10 retries of a batch record the batch's position in the stream in the
  queue `$PREFIX-cdc-failures`.
- **R36** (ubiquitous) The recipe shall let the function read its stream, write to its Firehose stream and send to
  its failures queue, and let Firehose use its bucket and its Glue database, and nothing else.
- **R37** (optional) Where `LAMBDA_ROLE_ARN` and `FIREHOSE_ROLE_ARN` are set, the recipe shall use those roles and
  create none.
- **R38** (event) When the changes of a run with `--target aws` are written, the recipe shall ask Athena for
  `trips_current` every 15 seconds until the changes show, and print how long that took.
- **R39** (complex) While the table or the Firehose stream of an earlier run is still being deleted, when a run
  with `--target aws` starts, the recipe shall wait until it is gone and create it again.

**Course constraints**

- **R40** (ubiquitous) The recipe shall start the name of every AWS resource it creates with `$PREFIX` (default
  `recipe`), with `_` in place of `-` in the Glue database.
- **R41** (ubiquitous) The recipe shall create its AWS resources in `ca-central-1` unless `AWS_REGION` names another
  Region.
- **R42** (ubiquitous) The recipe shall create its table with a maximum of 100 read and 100 write request units
  per second.

**Clean-up**

- **R50** (event) When `clean_up.py` runs, the recipe shall delete everything a run and the three extensions
  created for this `PREFIX`: schedules, alarms, the event source mapping, the function and its log group, the
  failures queue, the Firehose stream, the Glue tables and database, its objects and its bucket, the table, and its roles.
- **R51** (event) When `clean_up.py --dry-run` runs, the recipe shall print what it would remove and delete nothing.
- **R52** (unwanted) If AWS refuses a step of `clean_up.py`, then the recipe shall exit with code 1 after it has
  tried every other step and printed `FAILED` for the refused one.
- **R53** (event) When `clean_up.py` runs and a resource is already gone, the recipe shall pass over it without a
  failure.
- **R54** (ubiquitous) The recipe shall leave in place what it did not create: every alarm other than its two,
  every role without its tag, and in a bucket named by `BUCKET` every object outside its four prefixes and the
  bucket itself.
- **R55** (state) While the table, the Firehose stream or the event source mapping is being deleted, the recipe
  shall report it as already being deleted in a clean-up, without a second delete.
- **R56** (unwanted) If S3 answers that it did not delete some objects, then the recipe shall print `FAILED`, keep
  the bucket and exit with code 1.
- **R57** (ubiquitous) The recipe shall have S3 delete Athena's result files 7 days after they were written, in a
  bucket it created.

**Requirements of the extensions**, each in the README of its folder:

- 60 to 64, lag from change to queryable, with an alarm: [`ext-lag-alarm/README.md`](ext-lag-alarm/README.md#requirements)
- 70 to 73, rollback or schema change: [`ext-rollback-or-schema/README.md`](ext-rollback-or-schema/README.md#requirements)
- 80 to 84, compaction and snapshot expiry: [`ext-maintenance/README.md`](ext-maintenance/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal with credentials for your team sandbox: AWS
CloudShell, or your laptop after its sign-in. One file of this recipe runs inside AWS: `main/handler.py`, in
Lambda. The other moving parts are services AWS runs (DynamoDB Streams, Firehose, Athena, EventBridge Scheduler);
the scripts in your terminal create them and ask them questions.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS |
|---|---|---|---|---|
| Function | `main/handler.py` | the offline run and the tests call it directly | `main/infra.py` zips it and creates the function | yes: Lambda runs it on every batch of stream records |
| Creating the pipeline | `main/infra.py`, `main/config.py`, `main/sql/` | no | yes: table, roles, bucket, Glue database and table, Firehose stream, failures queue, function, event source mapping | no |
| Scenario and report | `main/run.py`, `main/athena.py` | `python -m main.run`: moto, Iceberg on disk and DuckDB (`main/lake.py`) | `--target aws`: writes the trips and asks Athena | no; the queries run in Athena |
| Lag probe and alarms | `ext-lag-alarm/lag.py` | a simulated clock | `--target aws`, each time you run it | no; CloudWatch evaluates the alarms |
| Rollback, schema change | `ext-rollback-or-schema/*.py` | a local Iceberg table | `--target aws`: PyIceberg in your terminal against Glue and S3 | no |
| Maintenance | `ext-maintenance/maintenance.py` | a local Iceberg table | `--target aws`; `--schedule` creates two schedules | the two schedules: EventBridge Scheduler starts the Athena queries every night; no code of yours |
| Clean-up | `clean_up.py` | no: it always calls AWS (`--dry-run` only looks) | yes, with the `PREFIX` of the run | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

This recipe does not have a container or an instance.




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole` or `dynamodb:TagResource`. Each Athena query also needs access to the Glue Data Catalog tables and the S3 data it uses, and write access to the query result location ([Identity and access management in Athena](https://docs.aws.amazon.com/athena/latest/ug/security-iam-athena.html)).

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StartQueryExecution`, `cloudwatch:DeleteAlarms`, `cloudwatch:DescribeAlarms`, `dynamodb:CreateTable`, `dynamodb:DeleteItem`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `firehose:CreateDeliveryStream`, `firehose:DeleteDeliveryStream`, `firehose:DescribeDeliveryStream`, `glue:CreateDatabase`, `glue:CreateTable`, `glue:DeleteDatabase`, `glue:DeleteTable`, `glue:GetDatabase`, `glue:GetTables`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListRoleTags`, `iam:PassRole`, `iam:PutRolePolicy`, `lambda:CreateEventSourceMapping`, `lambda:CreateFunction`, `lambda:DeleteEventSourceMapping`, `lambda:DeleteFunction`, `lambda:GetEventSourceMapping`, `lambda:GetFunction`, `lambda:ListEventSourceMappings`, `lambda:UpdateEventSourceMapping`, `lambda:UpdateFunctionCode`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `s3:CreateBucket`, `s3:DeleteBucket`, `s3:DeleteObject`, `s3:ListBucket`, `s3:PutBucketPublicAccessBlock`, `s3:PutLifecycleConfiguration`, `scheduler:DeleteSchedule`, `scheduler:GetSchedule`, `sqs:CreateQueue`, `sqs:DeleteQueue`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl` |
| `ext-lag-alarm/` adds | `cloudwatch:PutMetricAlarm`, `cloudwatch:PutMetricData`, `s3:PutObject` |
| `ext-maintenance/` adds | `scheduler:CreateSchedule`, `scheduler:UpdateSchedule` |
| `ext-rollback-or-schema/` adds | `glue:GetTable`, `glue:UpdateTable`, `s3:GetObject`, `s3:PutObject` |

### 1. Laptop, offline

Run these from the root of your clone. They do not need an AWS account. They need [uv](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
cd cdc-to-lake
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run
```

What you should see, line by line:

1. `uv sync` installs boto3, PyIceberg, DuckDB, pytest, behave and moto (a library that stands in for AWS inside
   the process).
2. `pytest` ends with a line like `52 passed`.
3. `behave` ends with `0 failed`; it takes about three minutes.
4. The run prints the report of [What you should see](#what-you-should-see).

Clean-up and cost: the offline run does not create anything and is not billed.

### 2. Sandbox terminal: the pipeline

Run by us on 2026-11-13 at 04:31 UTC (`output/main.txt`). Sign in to your team sandbox first, and check that the
account is the one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

To use roles you already have, export `LAMBDA_ROLE_ARN` and `FIREHOSE_ROLE_ARN` before the run.
`main/infra.py` `role_policies` lists what each must be allowed to do.

In `cdc-to-lake`, with a short name of your own in place of `yourname` (in CloudShell, install uv first:
`pip install --user uv`):

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python -m main.run --target aws
```

1. The run creates its own roles (`$PREFIX-cdc-lambda-role`, `$PREFIX-cdc-firehose-role`) unless you supply roles.
2. It writes three trips, changes one and removes one, then asks Athena every 15 seconds until the changes are
   queryable, and prints the same report as the offline run. In our run that took about 165 s with the 60 s buffer.

Clean-up and cost: the run keeps everything, so that the extensions can use it, and the function, the Firehose
stream and the bucket stay until you remove them. Clean up the same day, in the same terminal with the same
`PREFIX`: `uv run python clean_up.py --dry-run`, then `uv run python clean_up.py`. One run is under 0.01 USD
([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: the extensions

Each extension's README has its own part, with the sign-in, the commands, what you should see, the clean-up and the
cost: [lag alarm](ext-lag-alarm/README.md#run-it), [rollback or schema change](ext-rollback-or-schema/README.md#run-it),
[maintenance](ext-maintenance/README.md#run-it). All three were run by us on 2026-11-13 (UTC), after the main
scenario.

## What you should see

Offline (`output/main-local.txt`):

```
rows queryable before the buffer was written: 0
trips_current (latest state of each trip, from the change log):
  t-001  claimed    Kitsilano        18.50  driver=d-42
  t-002  requested  Downtown         12.00  driver=None
changes in the log: 5 for 3 trips
data last updated (newest change in the lake): 2026-11-12 23:59:51
longest change-to-arrival: 1 s
```

The changes are not queryable until the buffer is written. The changed trip shows its new status. The removed trip
is gone from `trips_current` but its `INSERT` and `REMOVE` rows stay in the log (5 changes for 3 trips). Times change on every run. On AWS (`output/main.txt`, a run on 2026-11-13 at 04:31 UTC) the report is the
same, with the first line replaced by the wait: changes were queryable after about 165 s with a 60 s buffer (polled
every 15 s), and the longest change-to-arrival was 61 s. The extensions' runs that followed it, and the clean-up, are in the same
folder.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Lag monitoring with an alarm | A probe item measures change to queryable; a CloudWatch alarm when it passes `LAG_LIMIT_SECONDS`; one on Lambda's `IteratorAge`; a `freshness.json` with "data last updated" for the dashboard | `ext-lag-alarm/` | After step 6: a probe trip, two alarms, one S3 object |
| Rollback or schema evolution | Undo a bad load with Iceberg snapshots (time travel, roll back, keep later good commits); add a column that old queries survive | `ext-rollback-or-schema/` | Step 5's table gains snapshots to return to; step 3's `ITEM_FIELDS` gains a column |
| Table maintenance on a schedule | Compaction (`OPTIMIZE`) and snapshot expiry (`VACUUM`), with Data scanned and run time before and after; two nightly schedules | `ext-maintenance/` | Step 5's many small commits are rewritten; two schedules run every night until clean-up |

## Cost and clean-up

Prices from the AWS bulk price lists for ca-central-1:

- Firehose Direct PUT into Iceberg tables: 0.083 USD per GB ingested in the first tier (price list usage type
  `CAN1-IcebergTablesBilledBytes`, operation `PutRecord`, publication 2026-11-04; the 0.050 USD rate is for a Kinesis
  Data Streams or MSK source). A run writes a few kilobytes.
- Athena: 5.50 USD per TB of Data scanned, with a 10 MB minimum per query (the minimum is from the Athena pricing
  page). A run makes about 10 queries
, so about 0.0006 USD.
- DynamoDB on-demand, Lambda and S3 for a few items: well under 0.01 USD. Whether the reads Lambda makes
  from the stream are billed is on the DynamoDB pricing page, not in the price list.

One run: under 0.01 USD. Nothing runs on a schedule in the main scenario; the stream
consumer runs only when there are changes.

```bash
PREFIX=recipe-yourname uv run python clean_up.py --dry-run
PREFIX=recipe-yourname uv run python clean_up.py     # schedules, alarms, function, log group, Firehose, Glue, S3 objects, table, roles
```

## Adapt it

- To use your own table, change `KEY` and `ITEM_FIELDS` in `main/handler.py`, the columns in `main/sql/create_table.sql`,
  and the key in `main/infra.py` `create_table`. `tests/test_schema.py` fails until the three agree.
- Write your own questions against `trips_current` (latest state) or `trip_changes` (history) in `main/sql/`.
  The offline test runs them in DuckDB first.
- To get fresher data, lower `BUFFER_SECONDS`, then add `ext-maintenance` for the small files that follow.
- A batch that still fails after 10 retries (11 tries) is given up, and its position in the stream is in the queue `$PREFIX-cdc-failures`:
  for a real system, read that queue, fetch the records from the stream within its 24 hours and fix or replay them.
- Several tables: one Firehose stream can route to several Iceberg tables; add one entry per table to
  `DestinationTableConfigurationList`.
