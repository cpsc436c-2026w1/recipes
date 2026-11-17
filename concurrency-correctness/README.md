# Two clients change the same DynamoDB item: one wins, the other is told

## What you build

Two drivers' apps accept the same trip at the same moment. Written without a condition, both apps are told they have
the trip; written with a conditional write, DynamoDB accepts one and refuses the other, and the refusal says which
driver holds the trip.

**Words used here.**

- **Read-modify-write**: the app reads an item, decides from what it read, and writes the item back.
- **Lost update**: two clients read the same version and both write. The second write replaces the first, and the
  first client's change is gone although it was told it succeeded (L12).
- **Conditional write**: a write that DynamoDB applies only if a condition on the stored item holds, checked and
  written in one step. On a refusal it raises `ConditionalCheckFailedException`.
- **Version number**: a number on the item that every write raises by 1. Writing "only if the version is still the
  one I read" is optimistic concurrency, also called compare-and-set.

## Use it when

- Your project takes the **Concurrency correctness** extension (2 points): two users, devices or functions may
  change the same record at the same time (accept a trip, book the last seat, move a balance).
- Add `ext-idempotent-retry/` when a client retries a write after a timeout and the write must not apply twice.
- Use a transaction (`TransactWriteItems`) instead when two items must change together; the conditional write here
  protects one item.

## Main scenario

1. `main/run.py` creates the table `$PREFIX-race-trips` (key `trip_id`) and puts a trip with status `requested`,
   version 1.
2. Two clients, `d-7` and `d-9`, each read the trip with a strongly consistent read and see version 1.
3. Both wait until the other has read (a barrier), so both decide from the same version.
4. Unsafe mode: both write `accepted` with their own driver id and no condition. Both writes succeed, so both drivers
   are told yes, and the table keeps whichever write came last.
5. Safe mode: both write with the condition `version = 1`. DynamoDB applies the first and refuses the second. The
   refused client gets the stored item back and tells its driver who has the trip.
6. The run repeats each mode `--rounds` times on fresh trips and prints the counts, then removes the table.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| How to stop a lost update (R10, R11) | conditional write on the version the client read | DynamoDB checks and writes in one step, so of two writes from the same version only one passes | lost update, conditional write: L12; [DDIA 2e ch. 8](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch08.html), "Weak Isolation Levels" (lost updates, compare-and-set); [DynamoDB condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html) |
| Version number or a status check (R11, R13) | version number (`version = :seen`) | it protects any change to the item, not only the accept: a fare change and an accept from two screens are refused the same way. For the accept alone, `attribute_not_exists(driver_id)` also works | optimistic concurrency: L12; [DynamoDB optimistic locking with version number](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/BestPractices_OptimisticLocking.html) |
| What the refused client learns (R12) | `ReturnValuesOnConditionCheckFailure = ALL_OLD` | the refusal carries the item that blocked it, so the app can tell the driver who has the trip without a second read | [returning the item of a failed conditional write](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html) |
| How the client reads before deciding (R14) | strongly consistent read | an eventually consistent read may return the trip as it was before the other driver's accept, and the client would then try a write that is bound to fail | eventual and strong reads: L12; [DynamoDB read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html) |
| Accept sent again (R16) | a refusal whose blocking item names the same driver is answered as accepted | the app sends an accept again when its reply was lost. The first one was applied and raised the version, so the second fails its condition. If told "refused, held by d-7", driver d-7 would be sent away from a trip that is theirs. The condition already keeps the second write from changing anything | [REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html) make mutating operations idempotent: a repeated request gets the answer of the first; [conditional write idempotence](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html) |
| Lock or no lock (R12) | no lock; the loser is refused and decides again | a lock held by a client that crashes blocks everyone else. A conditional write does not hold a lock | [DDIA 2e ch. 8](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch08.html), "Weak Isolation Levels": explicit locking and compare-and-set are both ways to prevent a lost update |
| How the test makes the race happen (R15) | a barrier: both clients read before either writes | without it the bad interleaving happens only by chance, so a test that passes proves little | course choice |
| Offline stand-in (R27) | moto, a library that stands in for AWS inside the process, with one call at a time (`main/local.py`) | DynamoDB checks a condition against the latest version of the item and writes in the same step ([conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html)); moto does not lock, so the wrapper gives the same promise. Read and write are still separate calls, so the race stays | course constraint: offline tests |
| Table (R30, R32) | on-demand capacity with a ceiling of 100 read and 100 write request units per second (course default), key `trip_id` | a short run does not need capacity planning. The trip is read and written by its id. DynamoDB aims to throttle a race loop left running with a large `--rounds` at the ceiling. At that rate, an hour of writes is at most about 0.25 USD per table (the extension has a second table with a ceiling of its own). DynamoDB applies the ceiling on a best-effort basis: it is a target, and short bursts can pass it | key from the access pattern: L12; DynamoDB Well-Architected Lens, cost optimization; [maximum throughput for on-demand tables](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode-max-throughput.html) |
| Run over what an earlier run left (R25, R26, R43) | a table that is there is used as it is; one that is still being deleted is waited out and created again; a clean-up waits until each table is gone | `CreateTable` refuses a name that exists with `ResourceInUseException`, and a deleted table stays in the `DELETING` state for a while | [CreateTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_CreateTable.html), [DeleteTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html); that a table in `DELETING` also refuses `CreateTable` is not on those pages |
| What counts as gone (R41, R42) | only DynamoDB's own "not found", `ResourceNotFoundException` | any other refusal is a `FAILED` step and exit code 1, so that a missing permission is not read as "nothing left" | [DeleteTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html) |

## Requirements

The requirements state what the recipe must do. Each is one sentence with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in parentheses
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
runs them offline. The requirements of the extension are in its own README, and their numbers go on from here:
[idempotent retry](ext-idempotent-retry/README.md#requirements) from 50. When you change what the recipe does,
change the requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they
do not match.

**Race**

- **R10** (event) When two clients accept the same trip in unsafe mode after both have read it, the recipe shall
  tell both clients that they have the trip.
- **R11** (event) When two clients accept the same trip in safe mode after both have read it, the recipe shall
  accept one of the two writes and tell the other client that it was refused.
- **R12** (unwanted) If a client's write is refused because the trip's version has moved on and the trip is held by
  another driver, then the recipe shall tell that client which driver holds the trip, without a second read.
- **R13** (event) When a safe-mode write is accepted, the recipe shall store the version the client read plus 1.
- **R14** (ubiquitous) The recipe shall read a trip with a strongly consistent read before a client decides.
- **R15** (ubiquitous) The recipe shall let both clients of a round read the trip before either of them writes.
- **R16** (unwanted) If a client's accept is refused and the trip is held by that client's own driver, then the
  recipe shall tell the client that it has the trip, without a second write.

**Run** (`main/run.py`)

- **R20** (event) When `main/run.py` runs, the recipe shall race 20 rounds in each mode, each on a trip of its own,
  unless `--rounds` names another number, and print for each mode how many rounds had one winner and how many told
  both drivers yes.
- **R21** (event) When the rounds are done, the recipe shall print one safe race on trip `t-0042` with what each
  driver was told and what the table holds.
- **R22** (event) When a run with `--target aws` ends, the recipe shall remove its table.
- **R23** (optional) Where `--keep` is given, the recipe shall leave the table in place.
- **R24** (unwanted) If the clean-up at the end of a run with `--target aws` fails, then the recipe shall print the
  failed step and exit with code 1.
- **R25** (complex) While the table `$PREFIX-race-trips` is in the `DELETING` state, when a run starts, the recipe
  shall wait until the table is gone and create it again.
- **R26** (event) When a run starts and the table `$PREFIX-race-trips` exists, the recipe shall use the table as it
  is.
- **R27** (ubiquitous) The recipe shall run with `--target local` without an AWS account or a network connection.

**Course constraints**

- **R30** (ubiquitous) The recipe shall start the name of every table it creates with `$PREFIX` (default `recipe`).
- **R31** (ubiquitous) The recipe shall send its calls to `ca-central-1` unless `AWS_REGION` names another Region.
- **R32** (ubiquitous) The recipe shall create each of its tables with a maximum of 100 read and 100 write request
  units per second.

**Clean-up**

- **R40** (event) When `clean_up.py` runs, the recipe shall delete the two tables of this `PREFIX` and wait until
  each is gone.
- **R41** (event) When `clean_up.py` runs and a table is already gone, the recipe shall report the table as
  `absent`, not as a failure.
- **R42** (unwanted) If AWS refuses a step of `clean_up.py`, then the recipe shall exit with code 1 after it has
  tried the other table and printed `FAILED` for the refused step.
- **R43** (state) While a table is in the `DELETING` state, the recipe shall wait for it in a clean-up without a
  second delete.
- **R44** (ubiquitous) The recipe shall leave in place every table other than its own two, also one whose name
  starts with `$PREFIX-race-`.

**Requirements of the extension**, in the README of its folder:

- 50 to 57, idempotent retry: [`ext-idempotent-retry/README.md`](ext-idempotent-retry/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal with credentials for your team sandbox: AWS
CloudShell, or your laptop after its sign-in. Nothing of this recipe runs inside AWS as your code: it does not have a Lambda
function, a container or an instance. The two clients are two threads of one Python process in your terminal, and
DynamoDB is the only AWS service they call.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS (Lambda, Fargate, EC2) |
|---|---|---|---|---|
| Race | `main/run.py`, `main/race.py`, `main/trips.py` | `--target local`: moto in place of DynamoDB (`main/local.py`) | `--target aws`: the table `$PREFIX-race-trips` in your sandbox | no |
| Retried write | `ext-idempotent-retry/run.py`, `tips.py` | `--target local` | `--target aws`: also `$PREFIX-race-requests` | no |
| Clean-up | `clean_up.py` | no: it always calls AWS | yes, with the `PREFIX` of the run | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

In a project, the client code of `main/trips.py` usually ends up in a Lambda function or a web app; for the same
conditional write inside a Lambda function, see [`lambda-dynamodb`](../lambda-dynamodb/README.md).




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `dynamodb:TagResource`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` |
| `ext-idempotent-retry/` adds | `dynamodb:DescribeTimeToLive`, `dynamodb:UpdateTimeToLive` |

### 1. Laptop, offline

From your local device (laptop), in a terminal at the root of your clone, run these commands. They do not need an AWS account. They need [uv](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
cd concurrency-correctness
uv sync
uv run pytest -q
uv run behave
uv run python main/run.py --target local
uv run python ext-idempotent-retry/run.py --target local
```

What you should see, line by line:

1. `uv sync` installs boto3, pytest, behave and moto (a library that stands in for AWS inside the process).
2. `pytest` ends with a line like `36 passed`.
3. `behave` ends with `0 failed`.
4. The race prints the table of [What you should see](#what-you-should-see): 20 rounds in each mode.
5. The retried write prints tip 10 for the naive write and tip 5 with the request id.

Clean-up and cost: the offline run does not create anything and is not billed.

### 2. Sandbox terminal: the race on DynamoDB

Run by us on 2026-11-12 (`output/main.txt`). Sign in to your team sandbox first, and check that the account is the
one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

In `concurrency-correctness`, with a short name of your own in place of `yourname` (in CloudShell, install uv
first: `pip install --user uv`):

```bash
export AWS_REGION=ca-central-1 PREFIX=recipe-yourname
uv run python main/run.py --target aws
```

What you should see: `removed  table recipe-yourname-race-trips`, then the same table of counts as offline. The run
creates one DynamoDB table and does not create an IAM role. `--rounds 50` changes the count. `--keep` keeps the table.

Clean-up and cost: the run removes its table itself. After `--keep`, or after a run that stopped early, run
`uv run python clean_up.py` in the same terminal with the same `PREFIX`. One run is under 0.001 USD
([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: the retried write

Use [idempotent retry](ext-idempotent-retry/README.md#run-it) to run the retried write. The extension's README
gives the sign-in, commands, expected output, clean-up and cost. The run was by us on 2026-11-12.

## What you should see

From a run with `--target local`. The run on AWS of 2026-11-12 is in `output/main.txt`: it prints the same table,
with `target: aws` and the two lines of its clean-up above it.

```text
target: local, table: recipe-race-trips
mode     rounds one winner both told yes winner is stored
unsafe       20          0            20                0
safe         20         20             0               20

one race on trip t-0042, safe mode:
  d-7: accepted
  d-9: refused, the trip is held by d-7
  table: driver d-7, version 2
```

What holds in every run: in unsafe mode every round tells both drivers yes; in safe mode every round has exactly one
winner, and the table holds the winner. `winner is stored` counts only rounds with one winner, so it is 0 in unsafe
mode. Which driver wins changes between rounds and runs.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Idempotent retry | a write that timed out after it succeeded is retried with the same request id and applies once | [`ext-idempotent-retry/`](ext-idempotent-retry/) | adds the table `$PREFIX-race-requests`; a rider's tip is written in a transaction with a marker for its request id |

## Cost and clean-up

One `aws` run costs under 0.001 USD (from the price list, not measured): about 125 write units at 0.6875 USD a million
and about 125 read units at 0.1375 USD a million (AWS bulk price list for ca-central-1).
A refused conditional write still consumes write units
([capacity units consumed by conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html)).
 The run removes its table. If a run stopped early, remove what
it left:

```bash
PREFIX=recipe-yourname uv run python clean_up.py
```

It attempts both tables and waits until each is gone: one that is refused is printed as `FAILED`, and the exit code
is then 1. Captured: [output/clean-up.txt](output/clean-up.txt).

## Adapt it

- **Your own item**: `main/trips.py` holds the item shape and the two writes. Keep a `version` attribute on every item
  that more than one client changes, add `ConditionExpression="version = :seen"` to every write of it, and set
  `version` to `:seen + 1` in the same write.
- **Your own refusal**: on `ConditionalCheckFailedException`, read the returned item (sent back because the write asks
  for `ALL_OLD`) and decide: tell the user, or read again and retry the change from the new version (with a retry
  limit).
- **Several items at once**: put the conditions and writes into one `TransactWriteItems` call; if any condition fails,
  the call does not write any item.
- **Names and region**: use `PREFIX` and `AWS_REGION` to adapt the names and region. Table names come from
  `trips.table_name` and `tips.requests_table_name`.
