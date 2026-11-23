# Nearby: drivers near a pickup point, and the nearest weather stations

> ⚠️ Where commands run: the offline tests and `DRY=1` on your laptop, in a terminal in the `dynamodb` folder of
> your clone; every command that calls AWS in a sandbox terminal (the recipe README, Where each part runs), in the
> `dynamodb` folder. The bench measures its times from where it runs, so run it in AWS CloudShell in ca-central-1,
> in a clone you make there.

Extension of the [dynamodb recipe](../README.md). Use this if your system looks items up by location in DynamoDB:
the nearest stations or places to a point, which do not move, or items that move, such as drivers. Each item is
stored under its **geohash cell** (defined below); a request reads its own cell and the cells around it by key,
and the code computes the exact distances. A nearby request never searches the whole table; only the replay's
check (step 4) scans every stored driver, to test the answers.

Words used here. A **geohash** is a short string that names a rectangle of the map; each added character splits the
rectangle into 32 smaller ones, and points close together usually share the first characters (`dr5ru7` is a block
near Times Square, about 611 m by 925 m). The **precision** is the number of characters. A **ring** `k` around a cell
is the cells `k` steps away from it in every direction: ring 1 is the 8 neighbours, ring 2 the 16 around those. The
**great-circle distance** is the shortest distance over the Earth's surface between two latitudes and longitudes.
The **ledger** (`ext-nearby/state/ledger.jsonl`) records each table the extension creates, so the clean-up deletes
only those. **p50, p95 and p99** are the response times at or under which 50, 95 and 99 of 100 requests fall; the
**service-level objective (SLO)** here is a p99 under 1 s.

## What it changes in main/

- Nothing in `main/`. Two more tables, made by `ext-nearby/nearby.py`: `$PREFIX-drivers-by-cell` (partition key
  `cell_type`, sort key `driver_id`) and `$PREFIX-points-by-cell` (partition key `cell_type`, sort key `point_id`).
  The partition key is `<cell>#<type>`, for example `dr5ru7#standard`.
- The table `$PREFIX-drivers` of [`ext-riders-and-drivers`](../ext-riders-and-drivers/README.md) gets five
  attributes on its drivers: `cell`, `car_type`, `lat`, `lon`, `position_time`. Run that extension first with
  `KEEP=1`.
- `clean_up.sh` runs `ext-nearby/clean_up.py --yes` first when this extension's ledger has a line.
- All the AWS work is in one Python module, `nearby.py` (standard library and boto3): it is what a Lambda handler
  would call. It needs a boto3 that knows `OnDemandThroughput` (1.44.2 does, 1.34.46 does not); with one that does not
  it stops before any call to AWS and says so.

## Steps

`run.sh` runs these, each printed before it runs:

1. Create the two cell tables (kept when they are there) and borrow `$PREFIX-drivers`.
2. Compute the 837 check answers of SIM01 (the simulated pickup requests, see Data) from the data alone, before AWS
   is called (`nearby.py expect`).
3. Put SIM02's 60 drivers (the moving drivers, see Data) at their start positions.
4. Replay SIM02's 7,200 positions and SIM01's 837 requests in time order at speed 5 (12 minutes for the hour). Each
   request runs with strongly consistent Queries and is compared with a Scan of every stored driver and with the
   answer computed in step 2.
5. Store SIM04's four weather stations (static points, see Data) under cells of a precision chosen from their spacing.
6. The 3 and the 4 stations nearest Times Square, then 4 with `MAX_RINGS=1`.
7. A point at the airport (`outside coverage`) and a car type `limo` (`unknown car type`): errors before any read.
8. The bench: SIM01's 837 requests one at a time, at most 15 a second; p50, p95 and p99, the p99 against 1,000 ms,
   and the read units.

Then the whole recipe's clean-up, unless `KEEP=1`.

### How a position is stored

`put_position(driver, car_type, lat, lon, time)` computes the cell at precision 6, reads the driver from
`$PREFIX-drivers` with a strongly consistent `GetItem`, and writes one transaction (`TransactWriteItems`):

1. an update of the driver in `$PREFIX-drivers`, with the condition that its stored `position_time` is older (or
   absent) and that its `cell` and `car_type` are the ones just read (or absent), and
   `ReturnValuesOnConditionCheckFailure = ALL_OLD`;
2. a put of the item `<cell>#<car_type>` / `driver_id` in `$PREFIX-drivers-by-cell`;
3. when the cell or the car type differs from the stored one, a delete of the old `<cell>#<car_type>` item.

When DynamoDB cancels the transaction, the code reads `CancellationReasons`. `ConditionalCheckFailed` on the driver
carries the stored driver: when its `position_time` is at least the arriving one, the call reports `older, kept`;
otherwise another position moved the driver first, and the call retries from the stored driver, at most 3 times. A
`TransactionConflict` or a throttle inside a cancellation is retried by the code, up to 5 attempts with a short
pause: the AWS SDKs do not retry a cancelled transaction
([transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html), "Transaction
conflict handling"). So two positions of one driver that both read cell A cannot leave the driver in two cells.

### How a request is answered

`nearby(lat, lon, car_type)` checks the point against the service area and the car type against the list, and
answers an error without a read when either fails. Otherwise it sends the 9 Queries on `<cell>#<car_type>` (the
point's cell and its ring 1) at once from a pool of 9 threads, computes the distance to each driver read, keeps
those within 500 m, sorts them nearest first and answers at most 5. An empty answer carries `radius_m: 500`.

`nearest(lat, lon, type, n)` for static points reads rings 0 and 1 (9 Queries at once), then ring 2, 3 … one ring at
a time. After ring `k` it keeps the points within `k` × the cell's shorter side (the **covered distance**); when it
has at least `n` of them, it answers the `n` nearest. A point read beyond the covered distance is not yet certain,
because a closer point may sit in the next ring. After `MAX_RINGS` rings (default 3) it answers what it has with
`complete: false` and the covered distance.

## Data

Every point and driver is simulated: the trip records the rides case starts from (the NYC Taxi and Limousine
Commission's High Volume For-Hire Vehicle records, Uber trips picked up in Manhattan in March 2024) give a pickup
zone for each trip, no coordinates and no drivers. `sim/generate.py` makes them, the same every time (seed 436),
around one reference point per pickup zone, chosen by hand inside each zone (zone 48: 40.7620, −73.9930; zone 161:
40.7549, −73.9840; zone 230: 40.7590, −73.9845; approximate).

| Set | What | Files under `sim/data/` |
|---|---|---|
| SIM01 rush-hour lookups (R90, R94, R95) | one nearby request per trip request of the rides case's rush hour: 837 requests, 304, 191 and 342 in zones 48, 161 and 230, between 09:00 and 10:00 UTC on 2026-11-23, each within 300 m of its zone's reference point; `standard` for about 80%, `xl` for the rest | `SIM01/requests.csv` (`request_id, car_type, lat, lon, event_time, pickup_zone_id`); `SIM01/nearby_expected.jsonl`, the check answers |
| SIM02 drivers moving (R80 to R82, R94) | 60 drivers, 20 per zone (16 `standard`, 4 `xl`), each sending a position every 30 s on a random walk of up to 15 m a step: 7,200 positions. 72 (1%) are delivered after the driver's next one, as injected faults; 69 of them arrive after a newer position and are refused (`older, kept`); 3 are followed by a late one too and are stored. 96 positions (1.3%) cross into another cell | `SIM02/drivers.csv` (start positions), `SIM02/positions.csv` (`delivery_id, driver_id, car_type, lat, lon, position_time, received_time`), `SIM02/_truth/events.jsonl` (each position as it happened), `SIM02/_truth/faults.jsonl` (one line per injected fault, the course layout: `{"fault": "late", "delivery_id", "driver_id"}`); `nearby.sim_answer()` computes the counts from them |
| SIM03 edge of coverage (R91, R93) | 5 points just outside the service area, 5 inside it more than 500 m from every SIM02 position | `SIM03/points.csv` |
| SIM04 weather stations (R100 to R102) | four static points of type `weather`, coordinates approximate and chosen by hand: `s-cpk` 40.7789, −73.9692; `s-lga` 40.7794, −73.8803; `s-jfk` 40.6386, −73.7622; `s-ewr` 40.6825, −74.1694 | `SIM04/points.csv` (`point_id, type, lat, lon`) |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| cells (R80, R90) | geohash cells of precision 6; a request reads its cell and the 8 around it; radius 500 m | the 3 × 3 block of precision-6 cells reaches at least 611 m in every direction from any point in the middle cell, so it holds every driver within 500 m. A precision-7 cell is about 153 m by 116 m: its block would reach only 116 m. One cell alone misses a driver across its edge: in the example of R90, `d-102` is 349 m away in the neighbour cell | key design from access patterns (L12; [DynamoDB access patterns](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-general-nosql-design.html)) |
| type in the key (R90) | `<cell>#<car_type>` as the partition key | a filter expression is applied after the items are read, and read units count the items read; with the type in the key a Query reads only the requested type | L12; [read and write units](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html) |
| 9 Queries at once (R90) | one Query per cell, sent in parallel | one after another, 9 round trips add up. `BatchGetItem` needs each item's full key, and the drivers in a cell are not known in advance. Each Query, also of an empty cell, uses read units (same page) | L12 |
| distance in code (R90, R101) | great-circle distance, Earth radius 6,371,008.8 m | DynamoDB finds items by key and does not search by distance; the cells bound what is read, the code decides what is near | L12 |
| errors before any read (R91, R92, R93) | `outside coverage`, `unknown car type` | an empty list then always means "no driver within 500 m inside the area", and the empty list names the radius searched | course choice |
| precision from spacing (R100, R101) | the finest precision whose cell's shorter side is at least the median nearest-neighbour distance; rings widened until the answer is certain | stations kilometres apart leave a 500 m search empty. With SIM04 the median nearest distance is 13,019 m, so precision 4 (shorter side 19,546 m) puts the three nearest stations of Times Square inside ring 1 | course choice |
| stop on covered distance (R101) | stop when `n` points lie within `k` × the shorter side | a point read in a corner cell can be farther than an unread point in the next ring: after ring 1, `s-jfk` is read at 23,036 m, beyond the 19,546 m covered, so a fourth station needs ring 2 | course choice |
| positions only (R90) | no driver status in the cell item | the status belongs to the accept flow of `ext-riders-and-drivers` (conditional writes). A system that needs only free drivers copies the status into the cell item and writes it in the accept transaction too | L12–13 |
| one transaction (R81, R82) | the driver, the new cell item and the old one, conditioned on the cell and type read | with a condition on the time alone, two positions that both read cell A and move to B and C would each delete A and leave the driver in B and C; with the cell in the condition the second one is refused, reads B and retries. A transactional write uses 2 write units per item up to 1 KB, also when it is cancelled: a position in the same cell is 4 units, a move 6 | L12–13; [transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html) |
| retries (R82) | botocore `mode='adaptive'`, `max_attempts=10`; the code's own retries of a cancelled transaction | a replay faster than real time meets the `MAX_UNITS` ceiling, set per table as 100 read request units and 100 write request units a second (`OnDemandThroughput`), and adaptive retries slow the client down; the SDKs do not retry a cancelled transaction, so a `TransactionConflict` inside one is retried by the code | [transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html); [retries](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html) |
| strong reads for the check only (R94) | eventually consistent Queries for requests, strongly consistent for the check | a strongly consistent read costs twice an eventual one. The dispatcher accepts that a position written a moment before may not show yet (L12); the check needs the promise of R94 | L12; [read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html) |
| a position every 30 s, written every time (R80) | every position is written | it keeps the distances exact. Writing only on a change of cell costs much less always on (see Cost) and makes a distance up to a cell off; a system that needs only the cell writes only on a change | course choice |
| replay (R94) | `nearby.py replay`, a loop of its own, at speed 5 | positions and nearby requests are not the trip events the course replayer sends. The loop sends them in time order, `speed` times faster than real time, and waits for each write, so no write is in flight at a request. At speed 5 SIM02 needs about 40 write units a second, under the 100 of `MAX_UNITS` | course choice |
| tables made by `nearby.py` (R110) | boto3 `create_table` in `nearby.py`, not the `table()` function of `lib.sh` | the tables carry the course tags (`Project`, `Team`, `Owner`) from `scripts/tags.py` as well as the recipe's `purpose` and `prefix`, and each create goes into the ledger that `clean_up.py` reads. The behaviour of `table()` is kept: on demand, the `MAX_UNITS` ceiling, a table that is there is kept, one that is being deleted is waited out, only "not found" means absent | course constraints (tags, clean-up) |
| clean-up by ledger (R113, R114) | `clean_up.py --yes` deletes only the tables its ledger records as created by this `PREFIX`; `$PREFIX-drivers` is borrowed and only its SIM02 drivers (`d-001` to `d-060`, which `seed` writes) lose the five attributes | a table the ledger does not record as created by this `PREFIX`, such as a teammate's or one another run made, is never deleted; the ledger is the clone's own file, so clean up from the clone that ran the recipe. `bash clean_up.sh --yes` deletes `$PREFIX-drivers` afterwards with the recipe's other tables; a bare `bash clean_up.sh` lists and deletes nothing | course constraints (clean-up) |
| the SLO (R95) | p99 under 1 s from the call of `nearby()` to its return, measured from a machine in ca-central-1 (CloudShell, or an EC2 instance) | that is the time a Lambda function in the Region would see; from a laptop, the network's round trip adds to it. The bench sends at most 15 requests a second (837 in about a minute, 67.5 read units a second): back to back, in the Region, it could pass the table's ceiling of 100 units a second, and DynamoDB throttles requests above it, so the p99 would measure the throttle ([maximum throughput exceeded](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/throttling-ondemand-capacity-exceeded-mitigation.html)). The percentile is the continuous one (DuckDB's `quantile_cont`); a failed request counts as infinitely slow | course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/nearby.feature`. A team keeps 2 to 5 lines per story; these are the recipe's.

**Positions of moving items**

- **R80** (complex) Where `ext-nearby` is used, when a driver's position arrives, the recipe shall store the driver
  in `$PREFIX-drivers-by-cell` under the key `<cell>#<car_type>`, where the cell is the geohash of the position at
  precision 6, with the latitude, longitude and time of the position.
- **R81** (complex) Where `ext-nearby` is used, if a driver's new position is in another cell, or has another car
  type, than the stored one, then the recipe shall delete the old cell item, write the new one and update the
  driver's cell and car type in `$PREFIX-drivers`, in one transaction that holds only if the driver's cell and car
  type are still the ones it read.
- **R82** (complex) Where `ext-nearby` is used, if that transaction is refused because the stored driver changed,
  then the recipe shall keep the stored position when it is at least as new as the arriving one, and otherwise
  retry from the stored driver, at most 3 times.

**Nearby requests**

- **R90** (complex) Where `ext-nearby` is used, when a nearby request arrives with a point and a car type, the
  recipe shall read the point's cell and its 8 neighbours with one Query each for that car type, sent in parallel,
  and answer the drivers within 500 m, nearest first, at most 5.
- **R91** (unwanted) If the point of a nearby request is outside the service area, then the recipe shall answer
  the error `outside coverage` without a read.
- **R92** (unwanted) If the car type of a nearby request is not one the system has, then the recipe shall answer
  the error `unknown car type` without a read.
- **R93** (event) When a nearby request inside the service area finds no driver within 500 m, the recipe shall
  answer an empty list with the radius it searched.
- **R94** (complex) While no position write is in flight, when a nearby request is read with strongly consistent
  Queries, the recipe shall answer the same drivers in the same order as a strongly consistent check of every
  stored driver and as the answer computed from the data before the run.
- **R95** (event) When `nearby.py bench` runs, the recipe shall send SIM01's requests one at a time and report the
  p50, p95 and p99 of their response times, the p99 against 1,000 ms, and the read units they used, counting a failed
  request as over 1 s.

**Static points**

- **R100** (event) When `nearby.py load` stores static points, the recipe shall choose the finest geohash precision
  whose cell's shorter side is at least the median distance from each point to its nearest other point, and store
  each point under its cell and type.
- **R101** (event) When a nearest-N request arrives, the recipe shall read the point's cell and its 8 neighbours,
  then each next ring of cells, until N points lie within the distance the rings read cover in every direction, and
  answer those N points, nearest first.
- **R102** (unwanted) If fewer than N points lie within the covered distance after `MAX_RINGS` rings (default 3),
  then the recipe shall answer the points within it, nearest first, with `complete: false` and the covered
  distance.

**Running and clean-up**

- **R110** (event) When `nearby.py tables` runs, the recipe shall create `$PREFIX-drivers-by-cell` and
  `$PREFIX-points-by-cell` on demand with a ceiling of `MAX_UNITS` request units a second and the course tags,
  record each in the ledger, and keep a table that is already there.
- **R111** (unwanted) If `$PREFIX-drivers` is not found, then the recipe shall exit with code 1, name the command
  to run first and create nothing.
- **R112** (event) When `ext-nearby/run.sh` runs with `DRY=1`, the recipe shall print its commands without a call
  to AWS.
- **R113** (event) When `ext-nearby/clean_up.py --yes` runs, the recipe shall delete only the tables its ledger
  records as created, remove its five attributes from the drivers of `$PREFIX-drivers`, and keep that table.
- **R114** (event) When `clean_up.sh --yes` runs and the ledger of `ext-nearby` has a line, the recipe shall run
  `ext-nearby/clean_up.py --yes` first.
- **R115** (event) When `ext-nearby/run.sh` ends without `KEEP=1`, the recipe shall run the clean-up of the whole
  recipe.

What it answers, in short: the nearby search never reads every item (it reads 9 cells by key, and a nearest-N
request reads further rings only until the answer is certain); an empty answer always means "nothing near inside the
area", because a point outside it and an unknown type are errors; static points get cells of a precision chosen
from their spacing, so the nearest stations come back even when they are kilometres away; and the cost of a run and
of a month is in Cost and clean-up.

## Assumptions

| What the case does not say | We assume | Affects |
|---|---|---|
| how often a driver's app sends its position | every 30 seconds | R80, cost |
| the area served | Manhattan: latitude 40.70 to 40.88, longitude −74.03 to −73.90 | R91 |
| the car types | `standard` and `xl` | R90, R92 |
| how many drivers are on the road in the three zones | 60, 20 in each zone | cost |
| how many stations a resident wants | 3, and never more than 3 rings of cells | R101, R102 |

## Run it

The capture of one live run is `../output/ext-nearby.txt`.

Run every command through `uv run`, so it uses the project's boto3: `nearby.py` needs one that knows
`OnDemandThroughput` (1.44.2 does). A system `python3` may have an older one (1.34.46 does not), and `nearby.py`
then stops with `FAILED: this boto3 (1.34.46) does not know OnDemandThroughput ... Nothing was created`.

In AWS CloudShell, ca-central-1, signed in to your lease, in the `dynamodb` folder of your clone. CloudShell has no
uv at first: install it once (it stays in your CloudShell home folder). Check the account first:

```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, once; then open a new CloudShell tab
aws sts get-caller-identity                   # "Account": your lease's account, before anything is created
```

The tables get the course tags `Team` and `Owner`; by default `Team` is your `PREFIX` and `Owner` your login name,
so there is nothing to set. Use one `PREFIX` of your own for every step and for the clean-up:

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
KEEP=1 bash main/run.sh                       # first, once
KEEP=1 bash ext-riders-and-drivers/run.sh     # the table $PREFIX-drivers stays
DRY=1 uv run bash ext-nearby/run.sh           # prints every command, runs nothing
KEEP=1 uv run bash ext-nearby/run.sh          # about 15 minutes; KEEP=1 keeps the tables for the calls below
```

Without `KEEP=1`, `run.sh` ends with the whole recipe's clean-up (R115): it deletes the parent recipe's tables too,
and the single calls below then fail. `SPEED=10` replays the hour in 6 minutes (about 80 write units a second, close
to the ceiling of 100). CloudShell ends a session after 20 to 30 minutes without keyboard or pointer input, and a
running process does not count ([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)):
press a key now and then during the run. If the session ends anyway, sign in again, run the bare `bash
clean_up.sh` to see which tables are left, and decide what to delete (Cost and clean-up).

The single calls, once the tables are there:

```bash
uv run python ext-nearby/nearby.py nearby 40.7580 -73.9855 standard
uv run python ext-nearby/nearby.py nearest 40.7580 -73.9855 weather 3
uv run python ext-nearby/nearby.py bench
uv run python ext-nearby/clean_up.py         # dry run: what the clean-up would delete
```

### AWS permissions

`dynamodb:CreateTable`, `DescribeTable`, `TagResource`, `DeleteTable` on the two cell tables; `GetItem`, `UpdateItem`,
`PutItem`, `DeleteItem`, `Query`, `BatchWriteItem` on the tables; `Scan` on `$PREFIX-drivers-by-cell` for the check
only. A transaction needs no permission of its own: DynamoDB checks the `PutItem`, `UpdateItem` and `DeleteItem`
permissions of the items in it
([IAM with transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)).
If your role refuses one of these, the first transaction (step 3, `seed`) stops with an `AccessDeniedException`
that names the action; run the bare `uv run bash clean_up.sh` to list what was made, delete it with `--yes`, and ask
the course staff for the item permissions it names. Do not replace the
transaction with separate writes: they cannot keep R81's promise, and a driver could be left in two cells.

## What you should see

The data and the code are fixed, so these counts are the same in every correct run, offline (moto) or live: step 4
prints `837 of 837 answers equal
nearby_expected.jsonl and the Scan check`, with 7,200 positions, 69 of them `older, kept`, and 96 moves to another
cell. Step 5 chooses precision 4 (median nearest distance 13,019 m, shorter side 19,546 m) and stores `s-cpk`, `s-lga`
and `s-ewr` under `dr5r#weather` and `s-jfk` under `dr5x#weather`. Step 6 answers `s-cpk` 2,699 m, `s-lga` 9,173 m,
`s-ewr` 17,626 m from ring 1 (9 Queries, covering 19,546 m); then `s-jfk` 23,036 m from rings 1 and 2 (25 Queries,
covering 39,092 m); with `MAX_RINGS=1`, the three with `complete: false`. Step 7 prints the two errors, with 0 units.
The bench (live only) reports p50, p95 and p99 for your run and compares the p99 with 1,000 ms; record your values,
they need not match. One run's output, for example (*measured*, `../output/ext-nearby.txt`): `PASS: p50 19.8 ms,
p95 22.9 ms, p99 33.8 ms against 1,000 ms over 837 requests`, 0 failures, 3,766.5 read units (837 × 9 Queries ×
0.5, the same in every run). After the replay each of the 60 drivers was in exactly one cell item, under the
cell and car type of its newest position in SIM02.

## Cost and clean-up

Prices: ca-central-1 on demand, Standard table class, 0.6875 USD per million write request units and 0.1375 USD per
million read request units (the course price sheet `436c-student/prices.md` on the course server, DynamoDB file
published 2026-11-11). Every cost cell is USD; a figure in brackets is CAD at 1 USD = 1.45 CAD, the course's
planning rate; 1 GB = 2^30 bytes. **Measured** (the units `ReturnConsumedCapacity` reported in the live run, `../output/ext-nearby.txt`): one run, the
row of the table below, used 29,239 write units (240 seed, 28,716 replay, 5 load, and 278 for the 69 cancelled
transactions, which no reply reports: 2 units per item, *estimated* from the
[transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html) page, "This
capacity is consumed even when a transaction does not succeed") and 21,930.5 read units (60 seed, 18,081 replay,
23 nearest, 3,766.5 bench): **0.023 USD (0.034 CAD), 0.08% of a 30 USD lease**. That figure is the measured units
plus the estimated units of the cancelled transactions, times the prices: an estimate, not a bill. The whole session
of that run (the parent's main/ and ext-riders-and-drivers, two benches, the clean-up) came to about 0.024 USD
(0.035 CAD). The 30-day and year columns are *computed* from those prices and the sizes
below. Items are about 110 bytes, and a cell's drivers of one
type stay under 4 KB (*assumed*: at most 32 standard drivers share a cell). 10% of positions move to another cell, an
upper bound (*assumed*; SIM02 has 1.3%). Free tier left out: storage under 0.1 MB, under 0.00003 USD a month at 0.275
USD per GB-month.

| Part | One run (SIM02's hour, SIM01's 837 requests with the check, one bench) | 30 days for demonstrations (10 hours) | 30 days always on (720 hours) | A year (demonstrations 120 hours; always on 8,760 hours) |
|---|---|---|---|---|
| writes: 6,480 positions in the same cell × 4 units and 720 moves × 6 units = 30,240 units an hour | 0.0208 | 0.208 | 14.97 | 2.50; 182.1 |
| writes: 60 drivers seeded, one transaction of 2 items each (240 units) | 0.0002 | 0.0002 | 0.0002 | 0.0002 |
| reads of the stored driver before each position, strongly consistent: 7,200 units an hour | 0.0010 | 0.010 | 0.71 | 0.12; 8.67 |
| reads of the requests: 837 × 9 Queries × 0.5 units = 3,766.5 units an hour | — (in the replay the requests are the check's strong Queries) | 0.005 | 0.37 | 0.06; 4.54 |
| reads of the check: 837 × (9 strong Queries × 1 unit + a strong Scan of the 60 cell items, 4 units measured) = 10,881 units | 0.0015 | (once) | — | — |
| reads of the bench, 3,766.5 units | 0.0005 | — | — | — |
| **Total, USD (CAD)** | **≈ 0.024 (0.035); measured 0.023 (0.034)** | **≈ 0.22 (0.32)** | **≈ 16.1 (23.3)** | **≈ 2.7 (3.9); always on ≈ 195 (283)** |

Against a 30 USD lease, 30 days of demonstrations are about 0.7% and a year about 9%; the same drivers left
running for 30 days would be about 54%, and for a year about 650%, almost all of it writes. At a 3% share of cell
changes, writing a position only when the driver changes cell brings the always-on 30 days to about 1.73 USD (0.64
writes, 1.09 reads). A replay that stops after the demonstration stops the cost; idle tables bill only storage.
Static points cost almost nothing: the four stations are written once, and a nearest request of ring 1 reads 9 × 0.5
units. With SIM02's own numbers (96 moves, 69 refused late positions, billed as 4 units each), the hour's writes are
28,992 units, about 0.020 USD; the live run measured 28,716 for the stored positions and 278 estimated for the
refused ones, 28,994. Each step prints its measured units (`ReturnConsumedCapacity`) at its end.

In CloudShell, in `dynamodb`, with the same `PREFIX`, choose one of the two. Each bare run lists what it would
delete and deletes nothing; read the list, then run it again with `--yes`.

This extension only (the two cell tables from the ledger, and the five attributes off the SIM02 drivers of
`$PREFIX-drivers`, which stays):

```bash
uv run python ext-nearby/clean_up.py          # lists; deletes nothing
uv run python ext-nearby/clean_up.py --yes    # deletes it
```

Or the whole recipe (runs the extension's clean-up first, then deletes the parent's tables, `$PREFIX-drivers`
included):

```bash
uv run bash clean_up.sh                       # lists; deletes nothing
uv run bash clean_up.sh --yes                 # deletes it
```

The clean-up's last check looks for the two table names and prints `(no table ... left)`; a name still there that
the ledger says this run created is a `FAILED` line and exit code 1. The ledger is `ext-nearby/state/ledger.jsonl`
(`LEDGER` names another); the recipes repository does not commit it, a team does.

## Adapt it

- Your own points: a CSV `point_id, type, lat, lon` and `uv run python ext-nearby/nearby.py load --points my.csv`.
- Your own service area and types: `AREA` and `CAR_TYPES` at the top of `nearby.py`.
- Another radius: keep the 3 × 3 block's reach above it. At precision 6 that is 611 m here; for a radius above that,
  use precision 5 (about 3,700 m) or read ring 2 as well.
- Near the poles or across the 180° meridian the neighbour rule above needs care; it is not needed for a city.
