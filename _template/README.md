# <Recipe title: the outcome, in words a student would use>

<!-- Template for every recipe. Keep the ten headings, in
this order: students and coding assistants look for the same things in the same place. Delete this comment. -->

## What you build

<Two sentences: what runs at the end, and what a person can see.>

## Use it when

<The project shape(s) or extension(s) this serves, and when another recipe fits better.>

## Main scenario

1. <One sentence per step: what happens, which service does it.>
2. ...

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| <for example, how the table is keyed> (<the requirements it serves: R10, R11>) | <the choice> | <one sentence> | <course constraint, or a named practice: lecture, book chapter, Lens best-practice ID> |

## Requirements

<What the recipe must do, one sentence each, in the EARS forms (Mavin et al., "Easy Approach to Requirements Syntax",
RE 2009). Start with the short table of the six forms from dashboard-chat: students have not seen EARS. The subject
of every sentence is "the recipe"; a script goes in the trigger ("When `clean_up.py` runs, the recipe shall ...").
Number them R10, R20, R30 ..., sub-requirements R11, R12 .... After the number, the form in brackets: ubiquitous,
event (When ...), state (While ...), unwanted (If ..., then ...), optional (Where an extension is used ...), complex
(two of these together). Use four or more of the six forms. One "shall" each. What the recipe does in response is
something a student can see: output, a record in AWS, an exit code. A number carries its source in an HTML comment
that starts with receipt:, facts:, measured:, ours:, unchecked or assumed. The requirements of an
extension go under the same heading in the extension's README, and their numbers go on from the last one here. Each
requirement has a behave scenario in features/ tagged with its number (@R20);
`python3 tools/check_requirements.py` keeps the two matched. A worked example: dashboard-chat.>

- **R10** (ubiquitous) The recipe shall <what always holds>.
- **R11** (event) When <trigger>, the recipe shall <response>.
- **R20** (unwanted) If <unwanted event>, then the recipe shall <response>.
- **R30** (optional) Where `ext-<slug>` is used, the recipe shall <response>.

## Run it

```bash
uv sync                      # on your laptop or in CloudShell
uv run pytest -q             # offline: no AWS account needed
uv run behave                # offline: the scenarios of the Requirements section
PREFIX=recipe-<you> uv run python main/run.py --target aws   # in your team sandbox, ca-central-1
```

### AWS permissions

<For a recipe that calls AWS: the IAM actions the student's sign-in needs, read from the code (boto3 calls, aws
commands, deploy templates), in a table with one row for the main scenario and clean-up and one row per extension
that adds actions. Name the roles the recipe creates in Decisions, not here.>

## What you should see

<A short part of the captured run in output/; say which values change between runs and which relations hold.>

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| <name> | <capability> | `ext-<slug>/` | <files and steps that change> |

## Cost and clean-up

One run costs about <amount> (<measured or from the AWS price list>). Remove everything it created:

```bash
PREFIX=recipe-<you> uv run python clean_up.py
```

## Adapt it

<What to change for your own data or project: which files, which names, which limits.>
