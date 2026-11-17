# Store for a ride app's live trips in DynamoDB

## What you build

Build a DynamoDB table that holds a ride app's live trips, read and changed the way the app's screens do it: one trip by its
key, a strong read where a stale answer would be wrong, and a conditional write so that two drivers cannot both
accept one trip. Each DynamoDB call prints the read or write units it used, which is what on-demand capacity bills.

## Use it when

- Your project is **operational first**, or any shape where an app reads and writes single records by id in
  milliseconds.
- Your project takes the **concurrency correctness** extension (2 points): two users may change the same record
  at once.
- Not for reports over the whole table: those belong in the lake. The extension `ext-nightly-report` shows how the
  table reaches it. A team that wants Python instead of the AWS CLI starts from
  [lambda-dynamodb](../lambda-dynamodb/), which runs the same reads and writes from a Lambda function.

## Main scenario

Words used here. A **table** holds **items**; each item is found by its **primary key**. The **partition key** decides
which storage partition holds the item; an optional **sort key** orders the items that share a partition key. A
**conditional write** happens only if a condition on the item holds. The condition check and the write happen in one step.

`main/run.sh` does these steps in your sandbox (L12, L13); "Run it" has the commands:

1. Create the table `$PREFIX-trips` with partition key `trip_id` and the global secondary index `waiting-by-zone`,
   billed per request (DynamoDB).
2. Load 50 requested trips, 25 per `batch-write-item` call, the most one call takes
   ([BatchWriteItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchWriteItem.html)).
   A call can write part of its batch and return the rest as unprocessed; the script sends those again.
3. The rider's screen reads trip t-0042 by its key, eventually consistent (the default).
4. The rider's screen reads the trip again with a strongly consistent read. It reflects every write that succeeded
   before it and uses twice the read units.
5. Driver d-7 accepts t-0042 with a conditional write: only if no driver is set yet.
6. Driver d-9 sends the same conditional write a moment later and is refused. The write asks for the blocking item on
   a refusal (`--return-values-on-condition-check-failure ALL_OLD`), so the error carries it and the app can tell d-9
   who has the trip.
7. The rider's screen reads the trip again with a strongly consistent read. The trip has driver d-7 and status accepted.
8. A dispatcher asks for every trip waiting at JFK. On the table no key names those trips, so a `Scan` reads all 50
   items and returns the 10 that match.
9. The dispatcher asks the same question through the index `waiting-by-zone` (partition key `pickup_zone`, sort key
   `status`), which step 1 created with the table. A `Query` reads the 10 waiting JFK trips and no others.
10. Clean up, unless `KEEP=1`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| keys (R10, R15) | partition key `trip_id`, from the read the app makes most often | every screen names one trip, so each read touches one item whatever the size of the table | key design from access patterns (L12; [DynamoDB access patterns](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-general-nosql-design.html)) |
| question with no key (R15, R18) | shown on purpose as a `Scan` (step 8), then answered through a global secondary index on pickup zone and status (step 9) | the Scan reads every item: 50 here, a million on a million trips. The index uses pickup zone as its partition key and status as its sort key, so the Query reads only the trips it returns. The index is not free: each write to the table that touches its keys is also a write to the index, and a read from it is eventually consistent. Its partition key, the pickup zone, has five values here: fine for a demo, and a hot partition on a busy table. The rule for a real index is a key with many values that the requests spread over ([partition keys that distribute the workload](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-uniform-load.html)) | same source; DynamoDB Well-Architected Lens, performance efficiency: [secondary indexes from the access patterns](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-wal.html); [read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html); `ext-riders-and-drivers` shows a second table for another question (a rider's trips) |
| conditional write (R12, R13) | `attribute_not_exists(driver_id)` on the accept | without it d-9's write silently replaces d-7's (a lost update); the check and the write are one step on the server | conditional writes (L12–13; [condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)) |
| consistency (R14) | eventual reads by default, a strong read after the accept | the strong read uses twice the read units; use it only where a stale answer is wrong | L12; [read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html); [read units](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html) |
| capacity (R10, R16, R32) | on demand (`PAY_PER_REQUEST`), with a ceiling of 100 read and 100 write request units per second on each table and on the index (course default, set in `MAX_UNITS`) | nothing to size, and a table with no requests is not charged for throughput (storage over the free 25 GB-months is billed): right for a sandbox and a small project. Without a ceiling, a loop that writes without end bills until someone notices. With a ceiling, DynamoDB throttles requests above the rate. An hour of such a loop is about 0.25 USD of writes for each table and for each index. Each index has a ceiling of its own. For the trips table, the total is about 0.55 USD an hour: each write is also a write to the index, and reads add 0.05 USD. DynamoDB applies the ceiling on a best-effort basis: it is a target, not a hard stop, and short bursts can pass it | course constraint: team sandboxes have budgets; [on-demand capacity mode](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html); DynamoDB Well-Architected Lens, cost optimization; [maximum throughput for on-demand tables](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode-max-throughput.html) |
| load that is cut short (R19) | items a `batch-write-item` call returns as unprocessed are sent again after 1, 2 and 4 seconds, in at most 4 calls (course default); if some remain, the run stops with `FAILED` | DynamoDB may write only part of a batch when it throttles, and says so only in the reply. A script that prints the count and goes on would run the rest of the scenario on fewer trips | [BatchWriteItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchWriteItem.html): retry unprocessed items with exponential backoff; DynamoDB Well-Architected Lens, reliability (retries with backoff); Serverless Applications Lens REL 2 (partial failures in batch processing) |
| names (R30, R33, R48) | every table starts with `$PREFIX` (default `recipe`) and carries the tags `purpose=recipe` and `prefix=$PREFIX` | teammates in one account give themselves different prefixes. `clean_up.sh` deletes only those names. The tags answer "whose table is this, and what is it for" in the console, and in the bill once the account's owner turns them on as cost allocation tags | course constraint; DynamoDB Well-Architected Lens, operational excellence (tags for identification and cost accounting) |
| AWS CLI in bash (R20, R21, R29) | each command printed before it runs; `DRY=1` prints and does not run anything | the commands are what you would type; read them first, then run them | course recipe format |
| second run (R24, R25, R45, R46) | a table that is there is kept; one that is still being deleted is waited out; the clean-up waits until each table is gone | `create-table` refuses a name that exists (`ResourceInUseException`), and a deleted table stays in the `DELETING` state for a while, so a run right after a clean-up would otherwise fail on its first command | [CreateTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_CreateTable.html), [DeleteTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html); that a table in `DELETING` also refuses `create-table` is not on those pages |
| what counts as gone or refused (R17, R26, R27, R43, R44) | only the API's own "not found" code: `ResourceNotFoundException` for a DynamoDB table, `EntityNotFoundException` for the Athena table and database in the Glue Data Catalog; a conditional write is "refused" only when its error is `ConditionalCheckFailedException` | any other refusal (no permission, an expired sign-in) would otherwise be read as "already gone" or "run main first", and a billable table or other resource would stay | [DeleteTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html), [Glue DeleteTable](https://docs.aws.amazon.com/glue/latest/webapi/API_DeleteTable.html), [Glue DeleteDatabase](https://docs.aws.amazon.com/glue/latest/webapi/API_DeleteDatabase.html) |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
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
[riders and drivers](ext-riders-and-drivers/README.md#requirements) from 50,
[nightly report](ext-nightly-report/README.md#requirements) from 60. When you change what the recipe does, change
the requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Main scenario**

- **R10** (event) When `main/run.sh` runs, the recipe shall create the table `$PREFIX-trips` with partition key
  `trip_id`, the index `waiting-by-zone` and on-demand capacity.
- **R11** (event) When `main/run.sh` loads the trips, the recipe shall write 50 trips in calls of 25 items each.
- **R12** (event) When two drivers accept the same trip, the recipe shall give the trip to the first driver only.
- **R13** (unwanted) If a driver accepts a trip that already has a driver, then the recipe shall refuse the write
  and print the item that blocked it.
- **R14** (event) When the rider's screen reads the trip after the accept, the recipe shall use a strongly
  consistent read.
- **R15** (event) When the dispatcher asks for the trips waiting in one pickup zone, the recipe shall print how many
  items the Scan read and how many it returned.
- **R16** (ubiquitous) The recipe shall print the read or write units of the load, of each read before the accept,
  of each accepted write, of the Scan and of the Query on the index, and say that a refused write reports none.
- **R17** (unwanted) If a conditional write fails for a reason other than its condition, then the recipe shall
  print `FAILED` after the error and exit with code 1.
- **R18** (event) When the dispatcher asks the same question through the index `waiting-by-zone`, the recipe shall
  print that the Query read only the items it returned.
- **R19** (unwanted) If DynamoDB returns items of a load call as unprocessed, then the recipe shall send those items
  again, in at most 4 calls for one batch, and exit with code 1 when items are still unprocessed after the 4th.

**Running the scripts**

- **R20** (event) When a script runs with `DRY=1`, the recipe shall print its commands without a call to AWS.
- **R21** (ubiquitous) The recipe shall print each AWS CLI command of a scenario before it runs.
- **R22** (event) When `main/run.sh` ends without `KEEP=1`, the recipe shall run the clean-up.
- **R23** (optional) Where `KEEP=1` is set, the recipe shall leave the tables in place and print the clean-up
  command.
- **R24** (complex) While the table `$PREFIX-trips` is in the `DELETING` state, when `main/run.sh` starts, the
  recipe shall wait until the table is gone and create it again.
- **R25** (event) When `main/run.sh` starts and the table `$PREFIX-trips` exists, the recipe shall keep the table
  as it is and run the scenario on it.
- **R26** (unwanted) If an extension starts and DynamoDB answers that the table `$PREFIX-trips` is not found, then
  the recipe shall exit with code 1 and print the command to run first.
- **R27** (unwanted) If AWS refuses a script's look for a table with any other error, then the recipe shall print
  that error and exit with code 1 without creating a table.
- **R28** (event) When an extension's run ends without `KEEP=1`, the recipe shall run the clean-up of the whole
  recipe.
- **R29** (unwanted) If the AWS CLI does not know `--cli-error-format`, then the recipe shall stop before it creates
  or writes anything and say that the AWS CLI must be updated.

**Course constraints**

- **R30** (ubiquitous) The recipe shall start the name of every table it creates with `$PREFIX` (default `recipe`).
- **R31** (ubiquitous) The recipe shall send its calls to `ca-central-1` unless `AWS_REGION` names another Region.
- **R32** (ubiquitous) The recipe shall create every table and index with a maximum of 100 read and 100 write
  request units per second unless `MAX_UNITS` names another number.
- **R33** (ubiquitous) The recipe shall tag every table it creates with `purpose=recipe` and `prefix=$PREFIX`.

**Clean-up**

- **R40** (event) When `clean_up.sh` runs, the recipe shall delete its three tables, the Athena table
  `trips_export` and the Athena database of this `PREFIX`.
- **R41** (optional) Where `BUCKET` is set, the recipe shall delete the export and the Athena results under
  `s3://$BUCKET/$PREFIX/` in a clean-up.
- **R42** (unwanted) If `clean_up.sh` runs without `BUCKET`, then the recipe shall delete nothing from S3 and print
  what stays there.
- **R43** (unwanted) If AWS refuses a step of `clean_up.sh`, then the recipe shall exit with code 1 after it has
  tried every other step and printed `FAILED` for the refused one.
- **R44** (unwanted) If AWS answers "not found" for a table or the database in a clean-up, then the recipe shall
  report it as already gone, not as a failure.
- **R45** (state) While a table is in the `DELETING` state, the recipe shall wait for it in a clean-up without a
  second delete.
- **R46** (event) When `clean_up.sh` has deleted a table, the recipe shall wait until the table is gone.
- **R47** (unwanted) If a table or the Athena database of this `PREFIX` is still there after the deletes, then the
  recipe shall print `FAILED: still there` with its name and exit with code 1.
- **R48** (ubiquitous) The recipe shall leave in place every table other than its own three, also one whose name
  starts with `$PREFIX-`.

**Requirements of the extensions**, each in the README of its folder:

- 50 to 55, riders and drivers: [`ext-riders-and-drivers/README.md`](ext-riders-and-drivers/README.md#requirements)
- 60 to 65, nightly report: [`ext-nightly-report/README.md`](ext-nightly-report/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal signed in to your team sandbox: AWS CloudShell
(which has the AWS CLI and Python),
or your laptop with AWS CLI v2. The scripts ask the AWS CLI for errors as JSON (`--cli-error-format json`); they
check for the option before they create anything and stop if your AWS CLI does not know it. We ran 2.39.3.
 Nothing of this recipe runs inside AWS as your code: it does not have a Lambda function, a
container or an instance. The scripts send requests to DynamoDB, and in the nightly report to S3 and Athena, which
are services AWS runs.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS (Lambda, Fargate, EC2) |
|---|---|---|---|---|
| Main scenario | `main/run.sh`, `lib.sh` | `DRY=1` prints the commands | yes: creates `$PREFIX-trips` and reads and writes it | no |
| Riders and drivers | `ext-riders-and-drivers/run.sh` | `DRY=1` | yes: two more tables | no |
| Nightly report | `ext-nightly-report/run.sh` | `DRY=1` | yes: starts the export and the Athena queries, then waits for them | no: DynamoDB writes the export and Athena runs the queries |
| Clean-up | `clean_up.sh` | `DRY=1` | yes, with the `PREFIX` (and `BUCKET`) of the run | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI: dry runs, and the real scripts with the AWS CLI against moto on localhost | never against AWS | no |

For the same reads and writes from a Lambda function, see [`lambda-dynamodb`](../lambda-dynamodb/README.md).




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. Each Athena query also needs access to the Glue Data Catalog tables and the S3 data it uses, and write access to the query result location ([Identity and access management in Athena](https://docs.aws.amazon.com/athena/latest/ug/security-iam-athena.html)).

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `dynamodb:BatchWriteItem`, `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:ListTables`, `dynamodb:Query`, `dynamodb:Scan`, `dynamodb:UpdateItem`, `glue:DeleteDatabase`, `glue:DeleteTable`, `glue:GetDatabases`, `s3:DeleteObject`, `s3:ListBucket` |
| `ext-nightly-report/` adds | `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StartQueryExecution`, `dynamodb:DescribeExport`, `dynamodb:ExportTableToPointInTime`, `dynamodb:ListExports`, `dynamodb:UpdateContinuousBackups`, `s3:AbortMultipartUpload`, `s3:PutObject`, `s3:PutObjectAcl` |
| `ext-riders-and-drivers/` adds | `dynamodb:DeleteItem`, `dynamodb:PutItem` |

### 1. Laptop, offline: the dry run

These commands do not need an AWS account. They need [uv](https://docs.astral.sh/uv/).

From your local device (laptop), in a terminal at the root of your clone:

```bash
cd dynamodb
uv sync
uv run pytest -q
uv run behave
DRY=1 bash main/run.sh
```

What you should see, line by line:

1. `uv sync` installs pytest, behave and moto (a library that stands in for AWS inside the test process).
2. `pytest` ends with a line like `27 passed`. With the AWS CLI installed, two of the tests run the real scripts
   against moto and take about a minute; without it they are skipped.
3. `behave` ends with `0 failed`. The features that run the scripts against moto are skipped without the AWS CLI.
4. The dry run prints the nine steps with every `aws` command under them, then the clean-up's commands, and
   does not call anything.

Clean-up and cost: these commands do not create anything and are not billed.

### 2. Sandbox terminal: the main scenario

Run by us on 2026-11-14 in a course sandbox, with the index, the ceiling, the repeated load and the tags
(`output/main.txt`, `output/clean-up.txt`).
From your sandbox terminal, sign in to your team sandbox first and check that the account is the one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

Replace `ana` with your name, so that your tables do not collide with a teammate's. To keep the table for an
extension, run `KEEP=1 bash main/run.sh` instead of the last line:

```bash
cd dynamodb
export PREFIX=recipe-ana AWS_REGION=ca-central-1
DRY=1 bash main/run.sh                        # prints every command, runs nothing
bash main/run.sh                              # runs the scenario and cleans up at the end
```

What you should see: the nine steps of [What you should see](#what-you-should-see), then the clean-up's three
`"DELETING"` lines and `(no table left)`.

Clean-up and cost: the run cleans up itself; after a run with `KEEP=1`, run `bash clean_up.sh` in the same terminal
with the same `PREFIX`. One run is well under one cent ([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: the extensions

Follow each extension's README for sign-in, commands, expected output, clean-up and cost: [riders and drivers](ext-riders-and-drivers/README.md#run-it), run by us on 2026-10-21;
[nightly report](ext-nightly-report/README.md#run-it), run by us on 2026-10-21. Both start from
`KEEP=1 bash main/run.sh`.

## What you should see

The captured run is [output/main.txt](output/main.txt) (2026-11-14, run with `KEEP=1`; account number masked as 111122223333). From it:

| Step | Captured |
|---|---|
| 1 | `"CREATING"` |
| 2 | `unprocessed: 0`, 50 write units per call: 25 trips, and 25 entries in the index |
| 3 | the trip, 0.5 read units |
| 4 | 1.0 read units |
| 5 | 3.0 write units: one for the trip, two for the index, whose entry is deleted and written again because `status` changed |
| 6 | `ConditionalCheckFailedException`, then the item that blocked it: `DRIVER_ID d-7`, `STATUS accepted` |
| 7 | driver d-7, accepted |
| 8 | returned 10, read 50, 2.0 read units |
| 9 | returned 10, read 10, 0.5 read units |

These relations hold on every run. The strong read uses twice the units of the eventual one. Exactly one of the two
accepts succeeds. The Scan reads every item, however few it returns. The Query on the index reads as many as it returns. A refused write still uses write units: at least
one, or as many as the existing item needs
([capacity units consumed by conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html)),
and the refusal does not say how many, so step 6 does not print a write-unit count. The script calls a write "refused" only when the
error is the condition's own (`ConditionalCheckFailedException`). Any other error ends the run with `FAILED`.

With the ceiling, the normal load can take the retry path: step 2 writes 50 trips to the table and 50 entries to
the index within about a second, and DynamoDB may hand some back as unprocessed. The script then prints
`sending them again` and the load still ends with every trip stored. The captured run did
not take that path: both calls came back with `unprocessed: 0`.

With the index the load uses two write units per trip, not one: a write to the table and one to the index.
For clean-up, use `bash clean_up.sh` from `dynamodb`. The captured runs of the two extensions are of 2026-10-21 and
predate three changes: the index and its second write unit; their last line names an older clean-up path; and the
nightly report stops with a `FAILED` line when an Athena query does not succeed.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| riders and drivers | a second access pattern (a rider's trips in time order) with a sort key; a transaction that keeps a trip and its driver in agreement; optimistic locking with a version number | [`ext-riders-and-drivers/`](ext-riders-and-drivers/) | two more tables; runs after `KEEP=1 bash main/run.sh` |
| nightly report | a report that Athena queries from an export of the table to S3, instead of a Scan of the live table | [`ext-nightly-report/`](ext-nightly-report/) | turns on point-in-time recovery; needs `BUCKET`; runs after `KEEP=1 bash main/run.sh` |

## Cost and clean-up

The captured run of the main scenario used about 104 write units and 5 read units (103 and 4 printed; the refused
accept and the read of step 7 do not print theirs and are counted as 1 each
). Each loaded trip is two writes,
one to the table and one to the index, and the accept, which changes `status`, is three.
It is well under one cent (ca-central-1 on demand:
0.6875 USD per million write request units, 0.1375 USD per million read request units; a few KB of storage is inside
the free 25 GB-months, AWS bulk price list).
The extensions state their own cost. From your sandbox terminal for the AWS run, with the same `PREFIX`, remove
everything the recipe made:

```bash
bash clean_up.sh                          # the tables, and the Athena table of ext-nightly-report
BUCKET=my-report-bucket bash clean_up.sh  # instead, if you ran ext-nightly-report: your bucket's name
```

The clean-up's last checks look for this recipe's exact table and database names and print `(no ... left)`.
A name still there produces a `FAILED` line. Captured: [output/clean-up.txt](output/clean-up.txt) (2026-11-14, without `BUCKET`).
It waits until each table is gone, attempts every step, and ends with `FAILED` lines and exit code 1 when AWS refused
a delete. Only the API's own "not found" counts as already gone. Without `BUCKET` it does not delete anything from S3 and
says so.

## Adapt it

1. Write down your app's reads first, one line each ("show trip X", "rider R's trips this week"). Each one needs a key
   or an index that names it; a read with neither is a Scan.
2. Change the table name, the key, the index and the item attributes in `main/run.sh` (step 1 and the item generator
   in step 2),
   the reads and writes of the later steps that use them, and the names in `lib.sh`. Run `DRY=1 bash main/run.sh`
   before the first real run.
3. Keep a condition on every write that two users can race on: copy `accept()` and change the condition.
4. For the same reads and writes from code, copy `../lambda-dynamodb/main/handler.py`.
