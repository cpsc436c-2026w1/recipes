# Redeploy a changed handler

Extension of the [lambda-dynamodb recipe](../README.md). After you change `main/handler.py` (a new action, a new
condition), the extension puts the new code into the Lambda function created by `main/run.sh` and calls the function once.

## What it changes in main/

Nothing. It needs the function: run `KEEP=1 bash main/run.sh` first.

## Steps

1. The script packages `main/handler.py` as a zip with `handler.py` at its top level.
2. If the function exists, the script replaces its code (`update-function-code`) and waits until the update is done.
   If the function does not exist, the script creates it with the role from `main/run.sh`, after a 10-second wait
   for a new role (`ROLE_WAIT` changes the wait).
3. The script calls the function once with an event and prints the reply.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| redeploy in place (R80, R81) | replace the code of the existing function | the URL, the role, the resource policy and the log group stay the same, so callers do not notice | L3 |
| test after deploy (R82) | one direct call with a known event | a broken handler shows at once, before an app calls it | L3 |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R80** (complex) Where `ext-redeploy` is used, when the function exists, the recipe shall replace the function's
  code with `main/handler.py` and wait until the update is done, without a change to its URL, its role or its
  permissions.
- **R81** (complex) Where `ext-redeploy` is used, when the function does not exist, the recipe shall create it with
  the role of `main/run.sh`, or with `ROLE_ARN` when that is set.
- **R82** (complex) Where `ext-redeploy` is used, when the code is in place, the recipe shall call the function once
  with a `get` event and print the status and the reply.

## Run it

Run by us on 2026-11-14 in a course sandbox (`../output/ext-redeploy.txt`): the update went from `InProgress` to
complete, and the call returned `status: 200` with no error. In a sandbox terminal (the recipe README says what that
is), sign in and check that the account is the one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is changed
```

In the sandbox terminal, in `lambda-dynamodb`, with the `PREFIX` of your `KEEP=1 bash main/run.sh`:

```bash
export PREFIX=recipe-ana AWS_REGION=ca-central-1
DRY=1 bash ext-redeploy/run.sh     # prints the commands only
bash ext-redeploy/run.sh           # after each change to main/handler.py
```

Clean-up: this extension does not create anything new; `bash clean_up.sh` removes the API as before. Cost: see below.

## What you should see

The script prints the function's state and the status of the update (`Active`, `InProgress` in the captured run).
It waits until the update is done. The call then returns `status: 200` without an `error` and prints the trip as JSON.
The first call after a redeploy starts a new environment: in the captured runs its `REPORT` line has an
`Init Duration` (484.52 ms). A `FunctionError` in
the second line means the new handler failed; its traceback is in the log group (`ext-metrics`).

## Cost

One request and a few milliseconds of Lambda time: well under one cent (0.20 USD per million requests, AWS bulk price
list for ca-central-1).
