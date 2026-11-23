# Cost and teardown: every resource on one list, what it costs, and nothing left billing

## What you build

Every create call of your scripts carries the course tags and writes a row to `state/ledger.jsonl`; one inventory
finds what you have by four routes and writes `docs/resources.md`; `scripts/cost.py` turns a run's measured units
into the cost of the run, of 30 days and of a year, in USD and CAD; `scripts/teardown.sh` calls every clean-up, the
hourly parts first, and proves with each service's own "not found" that nothing is left. A small demonstration
system (a stream, a function, a table) shows all of it.

## Use it when

Use this if your scripts create anything in AWS: every system does. The tags and the ledger (R10) and the inventory
(R20) are needed before the first deploy, by Checkpoint 2 (Tue Dec 1); the cost (R30 to R60) and the teardown (R70
to R74) by the final hand-in (Tue Dec 15), where `docs/resources.md` shows everything deleted. Copy the complete
folder into your repository (its `scripts/`, `sim/`, `features/`, `prices.toml`, `pyproject.toml` and `uv.lock`) and
run every command from the copied folder. The shared helpers `scripts/tags.py`, `scripts/lake.py` and
`sim/replay.py` are copies of `simulated-runs`' files, unchanged.

## Main scenario

1. `scripts/demo_up.py` creates the demonstration system: stream `<PREFIX>-cost-events` (provisioned, 1 shard),
   function `<PREFIX>-cost-count` (arm64, 128 MB) with its role, log group and event source mapping, table
   `<PREFIX>-cost-counts`, the lake bucket and workgroup `<PREFIX>-wg` (through `scripts/lake.py`), and the Glue
   database `<prefix>_cost` with a table over `raw/trip-events/`. Every create passes the tags of
   `scripts/tags.py` and appends a ledger row.
2. `scripts/demo_run.py` replays SIM01 (the simulated rush hour of `simulated-runs`: 837 trips sent as 2,515
   deliveries) to the stream at 10 times its speed and to the bucket (`sim/replay.py`), and counts the bucket's
   events with one Athena query.
3. `scripts/units.py` reads the units of each billed part (REPORT lines, the function's capacity lines, Athena's
   Data scanned, the stream's hours from the ledger) into `evidence/<run-id>/units.json`.
4. `scripts/cost.py` prices them from `prices.toml` and writes `evidence/<run-id>/cost.md`: the run, 30 days always
   on and for demonstrations only, and the year with the month that passes the cost limit.
5. `scripts/inventory.py` writes `docs/resources.md`: what the ledger, the team's tag, the schedule groups and the
   name prefix find, and what is not in the ledger.
6. A bare `scripts/teardown.sh` runs the inventory and lists what each step and the leftovers would delete; it
   deletes nothing. `scripts/teardown.sh --yes` runs every step of `scripts/teardown.toml` (hourly first, `lake.py
   down` last), deletes the ledger's `created` rows still found, asks every service again and writes
   `docs/resources.md` with each row `deleted`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Tags and the ledger (R10) | tags passed in each create call, and a ledger row for every create, also `borrowed` for what was there | `GetResources` does not return untagged resources ([GetResources](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/API_GetResources.html)) and some resources take no tags; the ledger also says what was borrowed, so the teardown never deletes the course catalog or a bucket another recipe made | course tags `Project`, `Team`, `Owner`; COST03-BP02, add organization information to cost and usage ([Well-Architected](https://docs.aws.amazon.com/wellarchitected/2025-02-25/framework/cost_monitor_usage_org_information.html)) |
| Schedules (R10, R70) | schedules in a tagged group `<PREFIX>-<recipe>` | the Scheduler tags only schedule groups ([TagResource](https://docs.aws.amazon.com/scheduler/latest/APIReference/API_TagResource.html)); deleting a group deletes its schedules | Scheduler docs |
| Glue databases and tables (R20) | found by the name prefix `<prefix>_` | Athena DDL passes no tags, and tables take none; the demonstration makes them with the Glue API, untagged, so the inventory shows the route | Glue docs |
| What "gone" means (R70, R71, R72) | each service answers "not found" for the resource's own name | `GetResources` returns "tagged or previously tagged" resources (same page), so its list gives candidates, not proof | a delete is proved by the service, not by a list of candidates |
| Teardown order (R70) | each recipe's clean-up first (it knows its order: the mapping before the function), hourly steps before the others, `lake.py down` last; then the ledger's leftovers | an hourly part bills while it exists, used or not (price sheet); the bucket holds the other recipes' prefixes until their clean-ups have run | delete the hourly resources first, to stop the charges that grow while you wait |
| Leftovers (R70, R73) | with `--yes`, delete a `created` ledger row still found, of a kind the teardown knows; a row found only by the team's tag is deleted only with `--tagged`; never a `borrowed` row, nor one found by its name prefix alone (both fail the teardown as still there); any other kind goes under "Delete by hand"; a bare run lists all of it first | the ledger says which script created a resource; the team's tag says only that one of the team's scripts did, and another recipe may still use it; a name prefix such as `recipe` may be someone else's; a kind with no known delete is a person's call | delete only what is surely this system's |
| A ledger from elsewhere (R70) | a ledger row whose ARN names another account, or whose `prefix` is not the current `PREFIX`, is skipped and printed | a copied ledger, or one from an earlier lease, names resources the teardown must not delete by name in this account | delete only what is surely this system's |
| A refused inventory route (R74) | the route is printed as refused and the teardown ends `NOT complete` with exit code 1 | what only that route finds is missing from the list, so "nothing left" cannot be shown | a delete is proved by the service, not by a list of candidates |
| Units (R30) | each unit marked *measured*, *computed* or *assumed*, and "not measured" for a billed part without a reader | shard-hours from timestamps and S3 requests from the scripts' counts are worked out, not read from a bill; a missing reader must show, not shrink the total | course cost tags |
| Glue DPU (R30) | a data processing unit (DPU) is Glue's unit of compute capacity, billed per DPU-hour; the recipe uses `DPUSeconds` when Glue returns it, else `ExecutionTime` × workers × the worker's DPU, at least 1 minute | the docs say the field is set for Flex and auto-scaling runs ([JobRun](https://docs.aws.amazon.com/glue/latest/webapi/API_JobRun.html)); a standard Glue 5.0 run returned it too (194 DPU-seconds for a 97 s run on 2 workers, measured, `../s3-event-etl/output/session-2026-11-23.txt`) | Glue docs |
| Where the price comes from (R40) | units × the course price sheet's rates in `prices.toml` | the free tier is shared by every account in the course organisation, so the bill is not a team's cost; the Cost Explorer API is charged per request and updated a few times a day ([Cost Explorer API best practices](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api-best-practices.html)) | price sheet, "How to use the sheet" |
| A part with no rate (R40) | "unpriced", and the total called a subtotal | a total that leaves a part out must say so | ours: no quiet zero |
| The horizon (R60) | a year from 4 busy months (a run a day) and 245 idle days, both columns, with the month that passes the limit | a busy and an idle month show the choice between always on and on demand | Checkpoint 1 question on a long-term horizon |
| Kinesis mode (R50) | provisioned, 1 shard | 720 × 0.0165 = 11.88 USD in 30 days against 720 × 0.048 = 34.56 USD on demand (price sheet), which is more than the 30 USD lease | COST06-BP01, perform cost modeling ([Well-Architected](https://docs.aws.amazon.com/wellarchitected/2023-04-10/framework/cost_type_size_number_resources_cost_modeling.html)) |
| USD to CAD (R40) | 1.45 CAD per USD | the course's planning rate; one rate keeps teams' numbers comparable | course planning rate |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall", in the EARS forms
(Mavin et al., "Easy Approach to Requirements Syntax", RE 2009):

| Form | How the sentence is built |
|---|---|
| ubiquitous | The recipe shall ... (it always holds) |
| event | When <something happens>, the recipe shall ... |
| state | While <something is the case>, the recipe shall ... |
| unwanted | If <something goes wrong>, then the recipe shall ... |
| optional | Where <a part is used>, the recipe shall ... |
| complex | two or more of these together |

The scenarios are in `features/cost_and_teardown.feature`, tagged with the numbers; `uv run behave` runs them
offline. A team keeps 2 to 5 of these per story, with its own numbers.

**Every resource on one list**

- **R10** (ubiquitous) The recipe shall give every resource a script creates the tags `Project=436c`,
  `Team=$TEAM` (default `$PREFIX`) and `Owner=$OWNER` (default your login name) where the service takes tags, and append to `state/ledger.jsonl` a row with its
  type, name, ARN, `created` or `borrowed`, the script and its clean-up step.
- **R20** (event) When `scripts/inventory.py` runs, the recipe shall write `docs/resources.md` with one row for each
  resource found in the ledger, by the tag `Team=$TEAM`, in a schedule group `<PREFIX>-…` or by the name prefix,
  with how it bills, its clean-up step and the routes that found it, and a section "Not in the ledger".

**What a run, a month and a year cost**

- **R30** (event) When `scripts/units.py` runs for a run, the recipe shall write to `evidence/<run-id>/units.json`
  the units of each billed part, each marked measured, computed or assumed with its source, and a row "not measured"
  for a billed part without a reader.
- **R40** (event) When `scripts/cost.py` runs on a run's units, the recipe shall write the cost of each billed part
  in USD and CAD with its rate and the date of the price sheet, a part without a rate as "unpriced", and the total
  as a subtotal when a row is unpriced or not measured.
- **R50** (event) When `scripts/cost.py` runs, the recipe shall write the cost of 30 days always on (every hourly
  part 720 hours, the run at the stated rate) and for demonstrations only (hourly parts for the demonstration
  hours).
- **R60** (event) When `scripts/cost.py` runs, the recipe shall write the cost of the horizon (365 days unless
  `HORIZON_DAYS` is set) from busy and idle months in both columns, with the month in which each passes the cost
  limit.

**Nothing left billing**

- **R70** (event) When `scripts/teardown.sh --yes` runs, the recipe shall run the inventory again, then each step of
  `scripts/teardown.toml` with the hourly ones first and `scripts/lake.py down` last, then delete what is still found
  and its ledger records created (with `--tagged`, also what only the team's tag finds), never a borrowed row.
- **R71** (unwanted) If a resource is still there after the teardown, then the recipe shall print
  `FAILED: still there` with its type and name and exit with code 1.
- **R72** (complex) While every resource the team created answers "not found", when the teardown ends, the recipe
  shall write `docs/resources.md` again with each row `deleted` and the time of that answer, borrowed rows `kept`.
- **R73** (unwanted) If the teardown finds a resource of a kind it does not know, then the recipe shall list it
  under "Delete by hand" with its ARN, leave it in place and exit with code 1.
- **R74** (unwanted) If an inventory route's list call is refused, then the recipe shall print the route as refused,
  end the teardown `NOT complete` and exit with code 1.

## Run it

> ⚠️ Commands run in two places. Offline commands run in a terminal on your laptop, in the copied recipe folder, and
> need no AWS account. Live commands run in a terminal with your lease's credentials active (a laptop terminal, or
> CloudShell with `uv` installed), in the same folder, in `ca-central-1`.

From your laptop, in a terminal in the recipe folder (offline):

```bash
uv sync                   # installs the dependencies; it should finish without an error
uv run pytest -q          # moto, no AWS account; every test should pass
uv run behave             # the scenarios of the Requirements; none should fail
```

Fix any failure before you deploy.

### Run it live

From a terminal with your lease's credentials, in the recipe folder. First choose a short lowercase prefix that no
other resource in your lease uses, set the region, and check the account and `uv`:

```bash
export PREFIX=cost-yourname
export AWS_DEFAULT_REGION=ca-central-1 AWS_REGION=ca-central-1
aws sts get-caller-identity          # the Account must be your lease's; stop if it is not
uv --version                         # install uv first if this fails (CloudShell does not have it)
```

`TEAM` (the `Team` tag) defaults to `PREFIX` and `OWNER` to your login name; set them only to override. The whole run
takes about 17 minutes (measured, `output/main.txt`). One command runs all of it, the deletes included:

```bash
bash scripts/live_run.sh
```

`live_run.sh` probes the list calls, creates the demonstration system, replays SIM01, measures the units, prices them,
writes the inventory, lists what the teardown would delete and then runs `scripts/teardown.sh --yes`; its output goes
to `output/main.txt`, and it ends with `teardown complete: 0 still there, 0 by hand` when nothing is left. If it stops
partway, run the bare `bash scripts/teardown.sh`, read the list, then `bash scripts/teardown.sh --yes`.

Or step by step, from the same terminal:

```bash
uv run python scripts/inventory.py --probe          # each list call once: "allowed" or "refused"
uv run python scripts/demo_up.py                    # the stream, function, table, bucket and Glue database
uv run python scripts/demo_run.py --run-id run-1    # the replay and one Athena query
uv run python scripts/units.py --run-id run-1       # evidence/run-1/units.json
uv run python scripts/cost.py --run-id run-1        # evidence/run-1/cost.md
uv run python scripts/inventory.py                  # docs/resources.md
bash scripts/teardown.sh                            # lists what it would delete; deletes nothing
bash scripts/teardown.sh --yes                      # deletes; exit 1 if anything is left
```

For your own system: call `tags(service)` in every create and `record(...)` after it, put one `[[step]]` per
recipe's clean-up in `scripts/teardown.toml`, and run `inventory.py` before each demonstration.

Another recipe's system, with its own `PREFIX` and ledger: a steps file names its clean-up, run in its own folder
(`scripts/teardown-backfill.toml`, `scripts/teardown-s3etl.toml`). Set that system's prefix and ledger on their own
lines, list, then delete:

```bash
export PREFIX=s3etl-yourname
export LEDGER=../s3-event-etl/state/ledger.jsonl
bash scripts/teardown.sh --steps scripts/teardown-s3etl.toml           # lists only
bash scripts/teardown.sh --yes --steps scripts/teardown-s3etl.toml     # deletes
```

A row found only by the team's `Team` tag and not in the ledger is listed as `left: tag only` and fails the teardown
as still there. Check that no recipe still uses it; then add it to the ledger, delete it by hand, or run the
teardown again with `--yes --tagged`.

### AWS permissions

| Part | Actions |
|---|---|
| inventory (read only) | `tag:GetResources`, `iam:ListRoles`, `iam:ListRoleTags`, `scheduler:ListScheduleGroups`, `scheduler:ListSchedules`, `glue:GetDatabases`, `glue:GetTables`, `athena:ListDataCatalogs`, `lambda:ListFunctions`, `lambda:ListEventSourceMappings`, `logs:DescribeLogGroups`, `sts:GetCallerIdentity` |
| demonstration system | `kinesis:CreateStream`, `kinesis:DescribeStreamSummary`, `kinesis:PutRecords`, `kinesis:AddTagsToStream`; `lambda:CreateFunction`, `lambda:GetFunction`, `lambda:CreateEventSourceMapping`, `lambda:TagResource`; `iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole`, `iam:TagRole`; `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, `logs:TagResource`; `dynamodb:CreateTable`, `dynamodb:DescribeTable`, `dynamodb:TagResource`; `athena:CreateWorkGroup`, `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`; `glue:CreateDatabase`, `glue:CreateTable`; `s3:CreateBucket`, `s3:PutObject`, `s3:PutLifecycleConfiguration`, `s3:PutBucketPublicAccessBlock`, `s3:TagResource` |
| units | `logs:FilterLogEvents`, `athena:GetQueryExecution`, `glue:GetJobRuns` |
| teardown | the deletes of each kind (`kinesis:DeleteStream`, `lambda:DeleteFunction`, `lambda:DeleteEventSourceMapping`, `logs:DeleteLogGroup`, `dynamodb:DeleteTable`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `glue:DeleteTable`, `glue:DeleteDatabase`, `athena:DeleteWorkGroup`, `s3:DeleteObject`, `s3:DeleteBucket`) and the asks (`s3:ListBucket`, which the `HeadBucket` call needs; `lambda:GetEventSourceMapping`, `iam:GetRole`, `glue:GetDatabase`, `glue:GetTable`, `athena:ListWorkGroups`) |

Run `uv run python scripts/inventory.py --probe` before you deploy: it makes each read-only list call once and prints
`allowed` or `refused` for each. A refused route leaves the inventory incomplete: the teardown then prints `FAILED:
inventory incomplete` and exits 1 rather than report nothing left. If a route is refused, check what it would list
in the console (for `tag:GetResources`, the Tag Editor; for the IAM route, the IAM console's roles) before you call
the teardown done. `iam:ListRoleTags` may be refused on roles your account came with: the inventory skips those
roles and says how many it skipped. Tags on an event source mapping at creation need `lambda:TagResource`; tags in
`CreateBucket` need `s3:TagResource`, and `scripts/lake.py` tags right after the create when they are refused.

## What you should see

The teardown of the demonstration system, offline (moto, account 111122223333; `t00` is the offline tests' prefix,
and your names begin with your own `PREFIX`):

```text
inventory 10 resources before the teardown
removed  Kinesis data stream t00-cost-events
removed  Lambda event source mapping t00-cost-events to t00-cost-count
removed  Lambda function t00-cost-count
...
step     demonstration system: exit 0
removed  Athena workgroup t00-wg
step     shared bucket and workgroups: exit 0
teardown complete: 0 still there, 0 by hand -> docs/resources.md
```

One live run's output, for example, is in `output/main.txt`: every probe call allowed, the demonstration system up,
SIM01 replayed, the inventory finding 10 resources, all by the ledger and a second route, none outside it, and the
teardown deleting them with every service answering not found. Your counts may differ. Check that every resource
you created appears in `docs/resources.md`, that each row reads `deleted` after `--yes`, and that the run ends
`0 still there, 0 by hand`. The same teardown, run with a steps file and another system's ledger, is in
`output/teardown-backfill.txt` and `output/teardown-s3etl.txt`.

## Data

DS01 is the TLC High Volume For-Hire Vehicle trip records of March 2024 (Uber trips picked up in Manhattan). The
demonstration replays SIM01 of `simulated-runs` ("rush hour", generated by `sim/generate.py SIM01` from
`sim/source/ds01_sample.csv`): 837 trips, 2,511 events sent as 2,515 deliveries, 4 requests delivered twice and 2
late; 841 of the deliveries are requests, and the function counts only those (837, the repeats cancelled).

## Cost and clean-up

Prices: USD, ca-central-1, free tier left out, from the course price sheet (page `436c-student/prices.md` on the
course server), copied with their date into `prices.toml`. Rates the sheet does not carry are from AWS's public price list
for ca-central-1, the source the sheet names: Kinesis (0.0165 USD a shard-hour, 0.0154 per million PUT payload
units), CloudWatch Logs ingestion (0.55 USD per GB) and the Glue Data Catalog (1 USD per million requests); data
transfer out is the sheet's earlier-read rate. The other rows of `prices.toml` (Scheduler, SQS, Fargate, load
balancer) are not used here and are *unchecked*. CAD at 1.45 per USD, the course's planning rate. Athena
bills per TB of data scanned; 1 GB = 2^30 bytes (the course convention), so `prices.toml` converts with
1 TB = 2^20 MB. The figures are
*computed* from those prices and the counts below, *assumed* where marked; the live run's units, further below, are
*measured*. They are subtotals: S3, DynamoDB and CloudWatch Logs storage is not included, and they are not a
spending cap.

| Part | One run (SIM01 at 10 times its speed, 14 minutes) | 30 days, always on (stream 720 h, a run a day) | 30 days, demonstrations only (10 h, 10 runs) |
|---|---|---|---|
| Kinesis shard, 1 shard-hour a run (each hour or part of one, *assumed*) | 0.0165 | 720 × 0.0165 = 11.88 | 10 × 0.0165 = 0.165 |
| Kinesis PUT payload units, 2,515 deliveries under 25 KB each | 0.000039 | 0.0012 | 0.0004 |
| Lambda requests, 360 batches (*assumed*; 468 measured) | 0.000072 | 0.0022 | 0.0007 |
| Lambda duration, 360 × 0.1 s × 128 MB = 4.5 GB-s (*assumed*) | 0.000060 | 0.0018 | 0.0006 |
| DynamoDB writes, 841 transactions × 2 items × 2 write units = 3,364 (a cancelled transaction bills too, *assumed*) | 0.0023 | 0.069 | 0.023 |
| Athena, 1 query at the 10 MB minimum | 0.000052 | 0.0016 | 0.0005 |
| S3 PUT and LIST, about 70 a run (*assumed*) | 0.00039 | 0.012 | 0.0039 |
| S3 GET, about 10 a run (*assumed*) | 0.000004 | 0.0001 | 0.00004 |
| Glue Data Catalog requests, about 20 a run (*assumed*) | 0.00002 | 0.0006 | 0.0002 |
| CloudWatch Logs ingestion, 1,440 lines × 200 bytes (*assumed*) | 0.00015 | 0.0044 | 0.0015 |
| Data transfer out, 0.3 MB of logs and units read on a laptop (*assumed*) | 0.00003 | 0.0008 | 0.0003 |
| Tagging API, IAM, describe and list calls (no rate on the price sheet) | unpriced | unpriced | unpriced |
| **Subtotal** (storage and the unpriced row left out) | **0.020 (0.028 CAD)** | **11.97 (17.36 CAD), 40% of 30 USD** | **0.20 (0.28 CAD), 0.7% of 30 USD** |

The year (365 days: 4 busy months of 30 days and 245 idle days; limit 30 USD, the lease budget). In a busy month the
always-on column makes a run a day (`RUNS_PER_DAY=1`, 30 runs), and the demonstrations-only column brings the system
up for 10 hours with one run each (`DEMO_HOURS=10`, 10 runs); an idle month has no runs:

| Column | Busy month | Idle month | A year | Passes 30 USD |
|---|---|---|---|---|
| Always on (the stream kept) | 11.97 | 11.88 | 144.91 (210.13 CAD), 483% of 30 USD | in the 3rd month |
| Demonstrations only (everything deleted between) | 0.20 | 0 | 0.78 (1.14 CAD), 2.6% of 30 USD | never |

**Measured** (the live run of 2026-11-23, `scripts/units.py` and `scripts/cost.py`; the replay to the stream took 823 s,
not 6 minutes: SIM01's deliveries span 110 minutes, a tenth of that is 11):

| Part | Units, *measured* | USD | CAD |
|---|---|---|---|
| Kinesis shard-hours | 1 (*computed* from the ledger's times) | 0.0165 | 0.0239 |
| DynamoDB write units | 2,932 (the function's ConsumedCapacity lines) | 0.0020 | 0.0029 |
| Lambda | 468 requests, 3.00 GB-seconds (REPORT lines) | 0.00013 | 0.00019 |
| Athena | 1 query, billed 10 MB | 0.00005 | 0.00008 |
| **One run** | | **0.0187** | **0.0271** |

30 days always on 11.95 USD (39.8% of 30 USD), demonstrations only 0.19 USD; the year 144.80 against 0.75 USD,
passing the limit in the 3rd month always on (*computed* from the measured run). The two teardowns of the other
systems made only list, describe and delete calls, which are unpriced here.

Almost all of the always-on cost is the one shard. Your own figures come from your own `units.json`: measure one run
at your size and give your stated rates (`RUNS_PER_DAY`, `DEMO_HOURS`, `BUSY_MONTHS`, `HORIZON_DAYS`,
`COST_LIMIT_USD`). Several recipes together: add their 30-day figures and count a shared resource once.

Remove everything:

From the terminal with your lease's credentials, in the recipe folder, with the same `PREFIX` the run used:

```bash
export PREFIX=cost-yourname
bash scripts/teardown.sh          # lists what it would delete; ends "Nothing deleted. Run again with --yes"
bash scripts/teardown.sh --yes    # deletes; ends "teardown complete: 0 still there, 0 by hand" or exits 1
```

`uv run python clean_up.py` lists the demonstration system and `uv run python clean_up.py --yes` deletes it alone; it
keeps the bucket and workgroups for `scripts/lake.py down`. The teardown runs `lake.py down --yes` last. That step
deletes the bucket and workgroups only when this ledger records them `created`, and refuses while another recipe's
objects are left outside `raw/trip-events/` and `athena-results/`; it does delete everything under those two
prefixes. If another recipe of your project still uses the bucket, remove the `lake.py down` step from
`scripts/teardown.toml` until that recipe is done, and list the bucket as kept in `docs/resources.md`; a kept bucket
still bills for what it stores.

## Adapt it

1. **Your creates.** In every script that creates something: `Tags=tags("<service>")` in the call and
   `record("<kind>", name, "created", arn=..., script=..., clean=...)` after it. Kinds the teardown knows are in
   `scripts/kinds.py`; add one there (its label, how its service is asked, how it is deleted) for a new kind.
2. **Your clean-ups.** One `[[step]]` per recipe in `scripts/teardown.toml`, `hourly = true` for those with a
   stream, a cluster, a load balancer or a task.
3. **Your prices.** Copy the rows you use from the price sheet (`436c-student/prices.md`) into `prices.toml`, with
   the date you read them. A part with no row is printed "unpriced" and its total a subtotal.
4. **Your units.** A reader per billed part in `scripts/units.py`; until then the part is listed "not measured".
