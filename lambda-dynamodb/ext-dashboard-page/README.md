# A page of requests by zone, and how fresh each zone is

Use this extension of the [lambda-dynamodb recipe](../README.md) if your system shows its numbers on a page and
has to show how fresh each number is. One Lambda function serves the page at `/` and its data at
`/api/zone-counts`, read from DynamoDB: per zone, the trips requested in each of the last 12 periods, the time of the
newest request counted, and "no data" where nothing was counted. A quiet zone shows its own age while the other
zones stay fresh; when every zone goes stale at once, suspect the feed (the slot list below shows which slots did
not load). Freshness says how old the newest counted request is, not whether ingestion is healthy.
Its **lake variant** is for pages whose numbers come from Athena: a job started every hour runs the summary
query and writes its result into the same table, so no route of the page starts Athena. In either variant the page
can also list the 15-minute slots of a polled feed (`SLOTS_TABLE`, from `streaming-ingestion/ext-source-poller`).
It matters for a demonstration and the final video: the page must be honest about old data.

Words used here. A **period** is one bar of the chart: 5 minutes for counts a stream consumer writes, a day for the
lake's `zone_counts`; the page shows the last 12. A **service-level objective (SLO)** is a response-time target, and
**p95** is the 95th-percentile time: 95 of 100 reads finish within it. A zone is **stale** when its newest counted request is older than `STALE_MINUTES`. A **one-time
schedule** runs once, at the time of its `at(...)` expression; a **universal target** is a schedule whose target is
any AWS API action, named `arn:aws:scheduler:::aws-sdk:<service>:<action>`. **Reserved concurrency** is a number of
concurrent executions set aside for one function, which also caps it.

## What it changes in main/

Nothing. It makes its own function `$PREFIX-page` with its own role, so the API of `main/` and its URL stay as
they are. It uses the shared helpers copied into this recipe: `scripts/tags.py` (tags and the ledger),
`scripts/run_log.py` (`$PREFIX-runs`), `scripts/lake.py` (`ensure_workgroup` for `$PREFIX-wg`) and the replayer
`sim/replay.py`; nothing is needed from another recipe's folder.

## Steps

1. `run.sh` creates the counts table `$PREFIX-zone-counts` if it is missing (a table your counting function already
   writes is used as it is), the page's role (`dynamodb:Query` on that table, and on `SLOTS_TABLE` when that is set
   at deploy time, and its own log group), the log
   group kept 7 days, the function (Python 3.12, arm64, 512 MB unless `MEMORY_MB` says otherwise, 10 s), the tagged
   schedule group
   `$PREFIX-dashboard-page` and the role the URL's deletion runs as. No public URL yet.
2. `run.sh load rush-hour` counts the simulated hour into the table through the course replayer, moved so that the
   hour ends now: the page is fresh in a video.
3. `run.sh serve` shows the page on `http://localhost:8080` with your own sign-in. Nothing is public.
4. `run.sh url-on 3` first makes a one-time schedule that deletes the function URL 3 hours later, and only then
   the URL (auth type `NONE`: anyone with the address can call it, and each call bills your lease) with its two
   permission statements. `run.sh url-off` deletes both at once. The schedule deletes the URL only: the two
   permission statements stay on the function, harmless without a URL, until `url-off` or the clean-up removes
   them.
5. Lake variant: `run.sh --lake` also makes the job `$PREFIX-dash-refresh` (128 MB, 120 s, no asynchronous retries),
   its role, and the hourly schedule. Each run takes a lock in `$PREFIX-runs`, runs one query in `$PREFIX-wg`, writes
   each count and each zone's newest time with a conditional `UpdateItem`, and records `finished`, `failed` or
   `skipped`.

| File | What it is |
|---|---|
| `page.py`, `page.html`, `slots.py` | the page function: routes, freshness, the page, the slot states |
| `refresh.py` | the lake job |
| `load_counts.py` | counts a simulated data set into the table (replayer target `load_counts:handle`) |
| `serve_local.py` | the same handler on localhost |
| `bench.py` | one read after a forced cold start, then 100 reads; each against the SLO |
| `deploy.py`, `run.sh` | deploy, `url-on`, `url-off`; `DRY=1` prints every call |

## Data

The page shows counts of trip requests from the course's rides data (DS01: the NYC Taxi and Limousine Commission's
High Volume For-Hire Vehicle trip records, Uber trips picked up in Manhattan in March 2024, 6,045,943 rows), as the
`simulated-runs` recipe turns them into events. The IDs below are this extension's.

- **SIM01 rush-hour**: the simulated hour of `sim/data/SIM01` (837 trips in zones 48, 161 and 230: 304, 191 and 342,
  09:00 to 10:00 UTC on 2026-11-23, with 4 repeated and 2 late deliveries), counted into 36 count items and 3
  `latest` items.
- **SIM02 quiet-zone**: each event of that hour once, without the 60 trips of zone 48 requested from 09:25 to 09:35:
  244, 191 and 342 trips.
- **SIM03 feed-stops**: the rush-hour deliveries without any event after 09:40:00, as if the feed broke.
- **SIM04 lake-days**: the trips of 30 days merged into `<prefix>_lake.trips` and counted into
  `<prefix>_lake.zone_counts` by a scheduled merge; the job reads its last 12 days. Offline, a fake Athena stands in.

**Item format** (`$PREFIX-zone-counts`, key `zone_id` a number, sort key `period_start` a string): count items
`zone_id`, `period_start`, `requested`, and `newest_event_time` from a stream counter; the lake job writes
`generation` (its run's start time, for its conditional writes) in place of `newest_event_time`. One `latest` item
per zone with `period_start` = `latest` and `newest_event_time`, the time of the newest request counted for the
zone in either variant (the lake job takes it from `requested_time` in `trips`): the page's `as_of`. **JSON** (`/api/zone-counts`):
`read_at`, `data_as_of`, `stale`, `age_minutes`, `message`, `periods` (12 start times), and per zone `zone_id`,
`values` (12 numbers or `null`), `as_of`, `age_minutes`, `stale`. **Slot list** (`/api/slots`): `day`, `read_time`,
`slots` (96 entries in slot order: `slot_start`, `slot_state`, and `record_total` for `loaded` and `late`).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| front door (R100, R200) | a Lambda function URL, read routes only | one function and one address; an HTTP API adds a resource and 1.11 USD (1.61 CAD) per million requests. Students used API Gateway in A1; a URL needs `lambda:CreateFunctionUrlConfig` and `lambda:AddPermission` (Run it live says what to do if they are refused) | L3; course price sheet `436c-student/prices.md` (API Gateway, Lambda) |
| where the HTML comes from (R100) | the same function | one origin, so the page's calls need no CORS settings; a public S3 website needs a public bucket policy and CloudFront is not on the course price sheet, and neither was used in a lease | [function URL settings](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html) (CORS for calls from another domain) |
| how long the URL is public (R200, R201) | only during a demonstration: its deletion is scheduled before it is made | read-only routes still bill the team, about 1.20 USD per million reads at 128 MB (Cost). Lambda throttles a URL only through reserved concurrency, but Lambda keeps 100 units unreserved, so in an account whose limit is under 100 nothing can be reserved (one lease measured 10 on 2026-10-22; check yours in Run it live); many callers would also throttle the stream consumer, which shares the same limit | [Lambda concurrency](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html); [one-time schedules](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html); [universal targets](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-universal.html) |
| no route starts Athena (R161, R170) | the lake job runs one query an hour | every Athena query bills at least 10 MB: 14,400 reads a month would bill at least 14,400 × 10 MB, about 0.76 USD (5.50 USD per TB scanned), and any caller of a `NONE` URL could start more; the job bills 720 queries a month whatever the page does | course price sheet `436c-student/prices.md` (Athena, 10 MB minimum); L11 |
| how old the data is (R120, R130, R140) | a `latest` item per zone, read by the same Query | after an outage longer than the window, the window is empty and the page would not know how old the data is; the `latest` item keeps the newest event time of any age. A table with no `latest` item at all (one a team's own counter writes) gets its `as_of` from the items read | ours: the page must tell a quiet zone from a broken feed |
| where freshness is computed (R120, R130) | in the JSON | the tests read the JSON with an exact answer; the page's script only draws | ours |
| reads (R160, R210) | one Query per zone, eventually consistent; the slot list one strongly consistent Query | a Query reads one zone's items by key; an eventually consistent read costs half a strong one, and a count a few seconds late is within the stale limit. The slot list follows source-poller's design (a slot just loaded is shown at once) | L12 |
| cold read (R110, R111) | boto3 imported and the DynamoDB client made at module scope, in the Init phase; the function at 512 MB (`MEMORY_MB`) | measured (Response time): with the client made on the first read, that read took 5.4 s at 128 MB and 1.2 s at 512 MB, because importing boto3 (3.4 s at 128 MB) and making the client (1.6 s) ran in the handler at 128 MB's share of CPU. Made in the Init phase, the same work took about 0.3 s at each memory size measured (the Lambda pages cited here do not say why Init ran faster than the handler at 128 MB). Init is billed. At 128 MB the cold read was 0.74 s inside Lambda and up to 1.0 s at the caller, no margin; at 512 MB 0.41 s and at most 0.62 s, and a warm read is billed 16 ms instead of 100 ms, so 512 MB also costs less (Cost); 1024 MB was no faster cold and costs more | [execution environment lifecycle](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html) (the Init phase runs the static code, at most 10 s; init time is billed; SDK clients made in static initialization); [memory](https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html) (CPU in proportion to memory, one vCPU at 1,769 MB; 128 MB only for simple functions); [Python handler](https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html) (initialize SDK clients outside the handler) |
| refused requests (R150) | 404 for an unknown path, 405 for a method other than GET, both before any DynamoDB call | the URL is public: a request the page does not know costs no read | `main/` keeps the same rule (R17); Serverless Applications Lens SEC 3 |
| writes of the lake job (R170) | each count and each `latest` item with a conditional `UpdateItem` (`generation < :g`, `newest_event_time < :t`) | `BatchWriteItem` takes no condition, so a job that started earlier and finished later would put back older counts beside a newer time; with the conditions it leaves both, and counts each as `kept` | [condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html); conditional writes, L12 |
| the job's run check (R180) | a lock item in `$PREFIX-runs` (a conditional write that holds only when `lock_until` is past), timeout 120 s, asynchronous retries 0 and none in the schedule | a run still waiting for Athena is not started twice, and a failed run is not repeated by Lambda's retries | course convention for scheduled jobs; L3, L11, L12 |
| stale limit (R130) | 10 minutes, set in `STALE_MINUTES` | two 5-minute periods with nothing counted; a lake chart sets its own, for example 1560 minutes (26 hours) for a daily summary | assumed: ask your analyst (Assumptions) |
| refresh (R190) | the page reads the JSON again every 60 s | a 5-minute period fills in within a minute of its end; Q010's 8 hours a day are 480 reads | ours |
| chart library (R100) | Chart.js 4.4.1 from cdnjs, pinned, with its integrity hash | the page in the video and at the final hand-in must draw the same way; the browser refuses a file that changed | [cdnjs](https://cdnjs.com/libraries/Chart.js/4.4.1) |
| deploy code (R200) | Python with boto3 (`deploy.py`), not the AWS CLI of `main/run.sh` | every create gets the course tags and a ledger line from the shared `scripts/tags.py` | course convention: tags and the creation ledger |
| the slot list (R210) | a route of this page in either variant, on when `SLOTS_TABLE` is set | the team shows one page in its video; the route only reads, by key | ours |
| the page and the agent | the page never calls an agent | an agent behind an `AWS_IAM` URL needs signed calls, which a public page cannot make without a key in the browser; a chat page is `dashboard-chat/ext-shiny-app` | ours |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/dashboard_page.feature`. A recipe has more lines than a team's story: a team keeps 2
to 5 per story.

- **R100** (event) When a browser sends GET `/`, the recipe shall return the page as HTML whose script reads
  `/api/zone-counts` and draws one chart of the trips requested in each zone for each of the 12 periods.
- **R110** (event) When a caller sends GET `/api/zone-counts`, the recipe shall return JSON with one value for each
  zone and each of the 12 completed periods of `PERIOD` before the read, without the period that holds the read, and
  `null` for a zone and period without a stored count.
- **R111** (event) When `bench.py` runs against the deployed page, the recipe shall first force a cold start of the
  page's function, read `/api/zone-counts` once, and fail the check if that read takes 1,000 ms or more, whatever
  the p95 of the reads after it.
- **R120** (ubiquitous) The recipe shall give in the JSON, for each zone, `as_of`, the event time of the newest
  request counted for that zone, and for the page `data_as_of`, the newest `as_of` of all zones, and `read_at`.
- **R130** (unwanted) If a zone's `as_of` is more than `STALE_MINUTES` before the read, or missing, then the recipe
  shall mark that zone `stale` with its age in whole minutes, and mark the page stale when its `data_as_of` is more
  than `STALE_MINUTES` before the read.
- **R140** (unwanted) If no zone has an `as_of`, then the recipe shall answer `data_as_of` `null`, mark the page stale
  and give the message "no data; freshness unknown".
- **R150** (unwanted) If a request has a method other than GET, or a path other than `/`, `/api/zone-counts` and
  (with the slot list) `/api/slots`, then the recipe shall answer 405 or 404 without a call to DynamoDB.
- **R160** (ubiquitous) The recipe shall read the counts with one eventually consistent Query for each zone, with no
  Scan.
- **R161** (ubiquitous) The recipe shall answer every route of the page without starting an Athena query.
- **R170** (complex) Where the lake variant is used, when its schedule fires, the recipe shall run the summary query in
  `$PREFIX-wg`, write its values and each zone's `as_of` into `$PREFIX-zone-counts` with writes that keep newer
  values, and record the run in `$PREFIX-runs`.
- **R180** (complex) Where the lake variant is used, if the previous run of the job still holds its lock, then the
  recipe shall record the run as `skipped` without starting a query.
- **R190** (state) While the page is open, the recipe shall read `/api/zone-counts` again every 60 seconds.
- **R200** (event) When `run.sh url-on <hours>` runs, the recipe shall create the one-time schedule that deletes the
  public function URL after that many hours before it creates the URL, and create no URL when the schedule cannot be
  made.
- **R201** (event) When `run.sh url-off` runs, the recipe shall delete the URL and its schedule at once.
- **R210** (complex) Where `SLOTS_TABLE` is set, when a caller sends GET `/api/slots?day=` with a UTC date, the recipe
  shall return each of the day's 96 slots in slot order with its state from one Query of the slot table (`missing`
  more than 30 minutes after a slot without an item ended, else `not due`), and 400 for a day that is not a date.


## Response time and assumptions

**Response time (SLO of R110 and R111):** under 1 s at p95 for GET `/api/zone-counts`, and under 1 s for the
first read after a cold start on its own, from the request leaving to the last byte of the answer. Load and
measurement: `bench.py` from CloudShell in ca-central-1 after `run.sh load rush-hour`: first a forced cold start (a
new value of `BENCH_COLD_START` in the function's environment) and one read, then 100 reads, one a second, the next
only after the answer; a non-200 answer counts as a failure and as over 1 s; DuckDB's `quantile_cont` at 0.95 over the
100 reads; it passes when the cold read and the p95 are both under 1,000 ms. One run's result, for example
(*measured* on 2026-11-23 from an EC2 instance in ca-central-1, at 512 MB): `cold read: 548.6 ms (HTTP 200): PASS`
and `100 reads, 0 failed; p50 30.5 ms, p95 40.4 ms: PASS` (`../output/ext-dashboard-page-cold-start.txt`). The first live run, at
128 MB with the client made on the first read, passed only warm (p95 113.6 ms): its one cold call took 5,354 ms
(`../output/ext-dashboard-page.txt`). Where that time went:

*measured* (2026-11-23, `../output/ext-dashboard-page-cold-start.txt`): 5 forced cold starts a row, each a call of
GET `/api/zone-counts` through the Lambda Invoke API (no URL), timed in ms from the `REPORT` lines, as median (max).
"At the caller" is the Invoke call timed on the calling EC2 instance; billed warm is the median of 15 warm reads.

| Memory | Client made | Init Duration | Duration | Init + Duration | At the caller | Billed, cold | Billed, warm |
|---|---|---|---|---|---|---|---|
| 128 MB | on the first read | 70 (91) | 5,407 (5,913) | 5,478 (6,004) | 5,602 (6,110) | 5,478 | 99 |
| 128 MB | in the Init phase | 362 (464) | 375 (395) | 741 (859) | 872 (995) | 742 | 100 |
| 512 MB | on the first read | 67 (84) | 1,146 (1,400) | 1,214 (1,467) | 1,315 (1,598) | 1,215 | 19 |
| **512 MB** | **in the Init phase** | **344 (346)** | **66 (77)** | **412 (415)** | **532 (618)** | **412** | **16** |
| 1024 MB | on the first read | 71 (73) | 575 (600) | 648 (671) | 770 (784) | 648 | 13 |
| 1024 MB | in the Init phase | 366 (460) | 43 (48) | 405 (508) | 531 (644) | 406 | 13 |

Made on the first read, importing boto3 took a median 3,427 ms and making the client 1,600 ms at 128 MB (662 and
378 at 512 MB, 328 and 194 at 1024 MB): CPU in proportion to memory. Made in the Init phase, the Init Duration grew by
about 0.3 s at every size. The billed cold time is Init + Duration: the Init phase is billed. The recipe takes the bold
row: the cheapest that meets the SLO cold with a margin (Decisions: cold read; Cost).

| Id | What the case does not say | Who would answer | We assume | Affects |
|---|---|---|---|---|
| Q010 | How long is the page open in a day? | the analyst | 8 hours, on one screen | R190, Cost |
| Q020 | How old may the data be before the analyst should not trust it? | the analyst | 10 minutes: two 5-minute periods | R130 |
| Q030 | Does anyone outside the team need to change data through the page? | the product owner | no; the page only reads | R150 |
| Q040 | How often must a lake chart be refreshed? | the analyst | every hour; a daily summary changes once a day | R170, Cost |

## Run it

### Laptop, offline

From your laptop, in a terminal in the `lambda-dynamodb` folder; nothing here calls AWS:

```bash
uv sync
uv run pytest -q tests/test_dashboard_page.py
uv run behave features/dashboard_page.feature        # @browser examples need: uv run playwright install chromium
DRY=1 bash ext-dashboard-page/run.sh --lake          # every call the deploy would make; nothing is called
DRY=1 bash ext-dashboard-page/run.sh url-on 3
```

`uv sync` installs the dependencies and `pytest` reports no failures. Both `DRY=1` commands print the AWS calls
they would make, each as `$ aws <service> <action> ...`, and make none. `behave` ends with `0 failed`; without Chromium the two `@browser` examples are skipped, and with it they open the
page in headless Chromium against the same handler, with a stand-in for the chart library and a fake clock.

### Run it live

The captures of the live examples are `../output/ext-dashboard-page.txt` (stream variant: `run.sh`, `load
rush-hour`, `serve`, `url-on 0.1`, `bench.py`, clean-up), `../output/ext-dashboard-page-cold-start.txt` (the cold
starts at 128, 512 and 1024 MB) and `../output/clean-up-ext-dashboard-page.txt` (the clean-up and its check).

From your laptop, in a terminal in the `lambda-dynamodb` folder, signed in with your lease's credentials. Check the
account first:

```bash
aws sso login                                 # or the sign-in your lease uses
aws sts get-caller-identity                   # "Account": your lease's account, before anything is created
aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions   # your account's concurrency limit
```

The last command prints your account's limit of concurrent executions in ca-central-1. The page and your stream
consumer share it, so during a demonstration watch `Throttles` of the consumer in CloudWatch.

Choose one variant per `PREFIX`. The stream variant, step by step (`PY=python3` makes `run.sh` use your own Python
if boto3 and pydantic are installed and uv is not):

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1   # your own prefix; the same for every step and the clean-up
bash ext-dashboard-page/run.sh                     # the table, the page function, its role; no public URL
bash ext-dashboard-page/run.sh load rush-hour      # the simulated hour, ending now
bash ext-dashboard-page/run.sh serve               # http://localhost:8080 in your laptop's browser; Ctrl-C ends it
```

`serve` runs the page on your laptop; open `http://localhost:8080` in the laptop's browser. In CloudShell,
`localhost` is CloudShell's machine, not your laptop, so skip `serve` there and use the URL below.

The public URL, for a demonstration. `url-on` prints the page's address as its last line (`The page is public until
... UTC: https://...`); copy it into `PAGE_URL`:

```bash
bash ext-dashboard-page/run.sh url-on 3            # public for 3 hours; prints the URL
PAGE_URL=https://YOUR_PAGE_ADDRESS/                # the address url-on printed
bash ext-dashboard-page/run.sh bench "$PAGE_URL"   # the cold read, then 100 reads, against the SLO
bash ext-dashboard-page/run.sh url-off             # when the video is done
```

`url-on` needs `lambda:CreateFunctionUrlConfig`, `lambda:AddPermission` and `scheduler:CreateSchedule`. If your role
refuses one, `url-on` prints a `FAILED` line, makes no URL (it deletes the schedule again if the URL fails), and you
keep using `serve` on your laptop. Before a demonstration, check that the scheduled deletion works in your lease:
run `bash ext-dashboard-page/run.sh url-on 0.1`, wait seven minutes, then run
`aws lambda get-function-url-config --function-name "$PREFIX-page"`. It should answer `ResourceNotFoundException`;
if it prints the URL, the schedule could not delete it: run `bash ext-dashboard-page/run.sh url-off` and use
`url-off` by hand after every demonstration.

The lake variant reads the lake of `cdc-to-lake/ext-scheduled-merge` (`<prefix>_lake.trips` and `zone_counts` in the
bucket `$PREFIX-lake-<account>`). Deploy that extension with the same `PREFIX` and let it merge first (its Run it
live), then deploy the page with the lake job, with the period and stale limit of a daily summary, and skip `load
rush-hour`:

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1   # the PREFIX of the scheduled merge
PERIOD=1d STALE_MINUTES=1560 bash ext-dashboard-page/run.sh --lake
```

The job `$PREFIX-dash-refresh` runs once an hour (`rate(1 hour)`), so its first run comes within the hour after
`run.sh --lake`. Before you open the page, check that run in the run log:
in the DynamoDB console, table `$PREFIX-runs`, the newest item of job `dash-refresh` should have `run_state`
`finished`. A `failed` run names its `failed_step` and `error_text`; an `AccessDeniedException` there means the
job's role may not query Athena in your lease, and the lake variant cannot run until that is allowed.

The slot list, in either variant: export `SLOTS_TABLE` with the name of your `ext-source-poller` table before
`run.sh`, so the page's role may query it too. Then GET `/api/slots?day=YYYY-MM-DD` (a UTC date) answers 200 with the
day's 96 slots.

## What you should see

Offline: the scenarios as above. Live, one run's output, for example (*measured*, `../output/ext-dashboard-page.txt`;
your times and durations differ): step 1 prints each create as
`$ aws <service> <action> ...` (a new role can take two retries before Lambda accepts it), and `(there already:
kept)` for what a second run finds; `load` prints `"requests": 837, "count_items": 36, "latest_items": 3`; the page
shows three series, a freshness line such as `data as of 00:59:58 UTC, read at 01:05:02 UTC`, and a table of the 12 periods
with "no data" where nothing was counted (in the run, the newest period, after the simulated hour ended); POST
answers 405 and an unknown path 404; `bench.py` prints `100 reads, 0 failed; p50 89.4 ms, p95 113.6 ms: PASS` (at
128 MB, before the cold read was added). At 512 MB (`../output/ext-dashboard-page-cold-start.txt`) it prints
`cold read: 548.6 ms (HTTP 200): PASS (under 1,000 ms)`, then `100 reads, 0 failed; p50 30.5 ms, p95 40.4 ms: PASS`.
The checks are HTTP 200, `0 failed`, and `PASS` for the cold read and for the p95. Six minutes after `url-on 0.1` the schedule had deleted the URL (`get-function-url-config` answered
`ResourceNotFoundException` within 21 s of the schedule's time), the old address answered 403, and the schedule had
deleted itself.

## Cost and clean-up

Prices: ca-central-1, the course price sheet (`436c-student/prices.md` on the course server, rates read
2026-11-18); USD with CAD at 1.45, the course's planning rate. 1 GB = 2^30 bytes. A Lambda GB-second is the memory in MB
divided by 1,024, times the seconds; DynamoDB rates are per million write request units (a write of up to 1 KB) and
read request units (a strongly consistent read of up to 4 KB, or two eventually consistent ones). *computed* from those prices and the sizes below, not measured; free tier left out. One run:
load SIM01, 20 page loads with their JSON reads, 1 cold call. 30 days: the page open 8 hours a day (Q010): 14,400
JSON reads, 300 page loads, 30 cold calls.

| Part | One run | 30 days | A year |
|---|---|---|---|
| DynamoDB writes, 39 items under 1 KB (0.6875 USD per million write request units) | 0.00003 | 0 (your counting function writes them) | 0 |
| DynamoDB reads, 3 Queries of 0.5 read units a JSON read (0.1375 USD per million read request units) | 0.000004 | 0.0030 | 0.036 |
| Lambda requests (0.20 USD per million) | 0.000008 | 0.0029 | 0.036 |
| Lambda duration, 512 MB: 16 ms a warm JSON read and a cold call billed 412 ms (measured, `../output/ext-dashboard-page-cold-start.txt`), 0.02 s a page (assumed) | 0.000008 | 0.0017 | 0.020 |
| Data transfer out, 12 KB a page and 2.5 KB a JSON read (assumed; 0.09 USD per GB) | 0.00002 | 0.0034 | 0.041 |
| CloudWatch Logs ingestion, 800 bytes a request (assumed; 0.55 USD per GB) | 0.00002 | 0.0060 | 0.073 |
| **Stream variant** | **0.00009 USD (0.0001 CAD)** | **0.017 USD (0.025 CAD)** | **0.21 USD (0.30 CAD)** |
| Lake job, hourly (Q040): 720 Athena queries at the 10 MB minimum, 7.03 GB (5.50 USD per TB) | 0.00005 a run | 0.038 | 0.46 |
| Lake job: 41 writes and 1 read a run, 2 S3 PUT and 2 GET, Scheduler 720 calls (1.10 USD per million), Lambda 5 s at 128 MB, logs 1 KB a run (assumed) | 0.00005 a run | 0.036 | 0.44 |
| Lake job: Glue Data Catalog requests, about 4 a run (assumed; 1.00 USD per million) | 0.000004 a run | 0.0029 | 0.035 |
| **Lake variant, page and job** | **0.0002 USD (0.0003 CAD)** | **0.094 USD (0.14 CAD)** | **1.14 USD (1.65 CAD)** |
| Slot list with the counts: one strongly consistent Query of 96 items (4 read units), Lambda 0.1 s at 128 MB or 0.025 s at 512 MB (the same GB-seconds), 6 KB out, 800 bytes of logs a read (assumed) | 0.00004 | 0.026 | 0.32 |
| **Lake variant with the slot list** | **0.0002 USD (0.0003 CAD)** | **0.12 USD (0.17 CAD)** | **1.46 USD (2.12 CAD)** |
| **Stream variant with the slot list** | **0.0001 USD (0.0002 CAD)** | **0.043 USD (0.062 CAD)** | **0.53 USD (0.76 CAD)** |

Against the 30 USD lease, 30 days of the page are under 0.1% and a year 0.7%; with the lake job 0.3% and 3.8% a
year; with the slot list as well 0.4% and 4.9% (stream variant with the slot list: 0.1% and 1.8%). Nothing here
bills by the hour; the page's cost grows with reads, about 1.14 USD (1.65 CAD) per million JSON reads (requests
0.20, duration 0.11, reads 0.21, transfer 0.21, logs 0.41), which is why the URL is public only during a demonstration. At
128 MB the duration row was 0.0025 USD a month and the duration of a million reads 0.17 USD: a warm read billed
100 ms at 128 MB and 16 ms at 512 MB, a quarter of the CPU for six times as long, so 512 MB costs less here.
Measure one JSON read's billed duration and read units and multiply by your own read rate.

*measured* (2026-11-23, stream variant, `../output/ext-dashboard-page.txt`): the units of the run, read from its
`REPORT` lines and CloudWatch, times the same price sheet. The run: `load rush-hour`, 1 page load and 102 JSON reads
through the URL (100 of them `bench.py`), 2 refused requests, 1 local read, 1 one-time schedule.

| Part | Measured units | USD | CAD |
|---|---|---|---|
| Lambda requests | 105 | 0.000021 | 0.000030 |
| Lambda duration, 128 MB arm64 | 14,593 ms billed (1.82 GB-s); median 90 ms a warm JSON read, 2 ms a refused request, 5,422 ms the one cold call | 0.000024 | 0.000035 |
| DynamoDB reads | 154.5 read units (1.5 a JSON read: 3 Queries of 0.5) | 0.000021 | 0.000031 |
| DynamoDB writes | 64 write units for the load of 39 items: each batch of the replayer writes again the count items it touched and each zone's `latest` item (`load_counts.py`); the computed row above counted 39 | 0.000044 | 0.000064 |
| CloudWatch Logs ingestion | 51,358 bytes (489 a call) | 0.000026 | 0.000038 |
| Scheduler | 1 invocation | 0.000001 | 0.000002 |
| Data transfer out | 100,357 bytes (931 a JSON reply, 5,395 the page; from the replies' sizes) | 0.000008 | 0.000012 |
| **The run** | | **0.00015** | **0.00021** |

That is 0.0005% of the 30 USD lease. From the measured units a million warm JSON reads cost about 0.88 USD (1.28
CAD): requests 0.20, duration 0.15, reads 0.21, transfer 0.08, logs 0.25; the computed 1.20 (at 128 MB) assumed
more log and reply bytes. That run was at 128 MB; at 512 MB the duration part is about 0.11 USD (0.16 CAD) a million.

The cold-start run (`../output/ext-dashboard-page-cold-start.txt`): 225 calls, 16.0 GB-s billed, 338 read units,
64 write units for the load: about 0.00035 USD (0.0005 CAD) with its logs left out.

Clean-up uses the recipe's `clean_up.py` with `--ext dashboard-page`: it removes this extension's resources only;
`main/`'s API (URL, function, log group, role) and what ext-read-path left stay. Without `--ext`, `--yes` also runs
`bash clean_up.sh --yes`, which deletes `main/`'s API: use that only when the whole recipe goes. From your laptop, in
`lambda-dynamodb`, with the same `PREFIX`:

```bash
uv run python clean_up.py --ext dashboard-page         # what it would remove; nothing is deleted
uv run python clean_up.py --ext dashboard-page --yes   # the URL first, then the schedule group, functions, log groups, roles, tables
uv run python clean_up.py --check                      # by tag, by schedule group and by name: what is left of this recipe's names
```

The bare run lists every deletion and deletes nothing; read the list, then run it with `--yes`. It deletes the tables and roles the ledger `state/ledger.jsonl` records `created` and keeps borrowed
ones (a counts table your counting function writes, a run log another recipe made). Every delete is attempted; a
refused one is a `FAILED` line and exit code 1. `--check` lists this recipe's own names only: the workgroup
`$PREFIX-wg`, borrowed tables and the upstream recipes' resources (the scheduled merge, the stream consumer) are not
in it and can still bill for storage or scheduled queries. The workgroup goes with `uv run python scripts/lake.py
down` (it lists first; `--yes` deletes it when the ledger records it created); clean up the upstream recipes with
their own clean-ups.
