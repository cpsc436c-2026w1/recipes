# Write behind a person's confirmation

Read the recipe README first. This extension builds on `main/` and gives an agent tools that must change data,
under L17's rule that a write waits for a person.

## What it changes in the main scenario

A third tool, `record_refund`, sits beside `get_schema` and `query`. It writes one row to `output/refunds.duckdb`
only when the call carries a confirmation code that a person made for exactly that change.

1. The model returns a tool call `record_refund` with a request id, an order id, an amount and a reason, and no
   code. App code checks that the order exists and the amount is at most the order's amount, writes nothing, and
   returns a tool result describing the change and the `approve.py` command for it.
2. The model shows the change to the person. The person reads it and runs `approve.py` on their own machine, which
   asks yes or no and prints a code such as `ana.1763075400.3f9a1c2b4d5e6f70`: the approver, the time the code
   stops working (15 minutes later, in seconds since 1970) and the signature.
3. The person pastes the code into the chat. It reaches the model as a user message.
4. The model returns the same tool call with `confirmation` set to the code. The server checks the code against
   these exact arguments and writes the row with the approver's name and the time.
5. A retry with the same request id does not write a second row and returns `already_applied`. The same request id with a
   different amount is refused.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Who approves (R82, R83, R87) | A person, outside the chat, with `approve.py` | The model cannot approve its own write. The code reaches it only from the person | Agentic AI Lens [AGENTSEC04-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec04-bp02.html) (human in the loop for critical decisions: a write pauses for a reviewer); L17 (the shopper confirms before the write tool runs; the model cannot skip it) |
| What a code approves (R82, R88, R89) | HMAC-SHA256 over the approver and the exact change, under `APPROVAL_SECRET` | The model does not hold the secret, so it cannot make a code; a code for 12.50 CAD does not fit 40.00 CAD | Course choice; GENSEC05-BP01 (least privilege for agentic workflows) |
| How long a code works (R90, R91) | 15 minutes from the moment the person made it (`APPROVAL_TTL_S`, course default). The expiry is part of the signed text, so changing it invalidates the code. `approve.py` sets it by the clock of the approver's machine and the server checks it by its own: two clocks that differ by a minute make a code live a minute longer or shorter | A code that works for ever can be used whenever the chat log is read again, by a person or by a model that finds it in its context | AGENTSEC04-BP02 (a decision within a defined time window; approval without a timeout policy is its third anti-pattern) |
| What the person sees (R81, R87) | The order, the amount, the reason and the request id, before the yes or no | The person must see the change before approving it | AGENTSEC04-BP02 (a reviewer who sees only the proposed action, without enough context, is its second anti-pattern) |
| Checks before approval (R86) | Order exists; 0 < amount ≤ the order's amount; reason and request id lengths | A person should not be asked to approve a change that cannot be right | Course choice |
| Audit (R83) | The name the approver gave to `approve.py` and the time in Coordinated Universal Time (UTC) are stored with the row. The name is what the person typed. The code proves that someone with the approval secret approved, but not who | You can answer "who approved this, and when", as far as the people who hold the secret are honest about their names | AGENTSEC04-BP02 (log each approval with its time and the reviewer's identity) |
| Idempotent by request id (R84) | The request id is the primary key; `INSERT ... ON CONFLICT DO NOTHING`; a repeat returns `already_applied` | An agent retries after a timeout; the retry must not refund twice | L12 (conditional writes; an idempotent load); L17 (idempotent writes: an idempotency key on a retried write, a step that runs twice must write once); see [`concurrency-correctness/ext-idempotent-retry`](../../concurrency-correctness/ext-idempotent-retry/README.md) for the DynamoDB form, `attribute_not_exists(request_id)` |
| Request id reused for another change (R85) | Refused with `conflict`; no row is written | A reused id with new arguments is a bug in the caller, not a new refund | Course choice |
| Tool annotations (R80) | `read_only_hint=False`, `destructive_hint=False`, `idempotent_hint=True` | A client can ask the person before the call. The server checks the code whether or not the client asks | [MCP specification, tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) (annotations; it also says there should always be a person who can deny a tool call) |
| Where writes go (R80, R83) | A separate DuckDB file; the read tools still read the in-memory copies | The read path stays locked; the write path is one table with one rule | GENSEC01-BP03 (least privilege on data stores) |

MCP lets the server ask the person a question through the client. This interaction is called elicitation
([MCP specification, elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation)). When your client supports
it, that can replace the copy-and-paste of the code; keep the code check on the server either way.

## Requirements

EARS has six forms. The forms and the form of these sentences are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/confirmed_write.feature`.

- **R80** (optional) Where `ext-confirmed-write` is used, the recipe shall offer a client a third tool,
  `record_refund`, not marked read-only, beside the two read tools.
- **R81** (complex) Where `ext-confirmed-write` is used, when `record_refund` is called without a confirmation
  code, the recipe shall return `needs_confirmation` with the change and the command a person runs to approve it,
  and write nothing.
- **R82** (complex) Where `ext-confirmed-write` is used, if the confirmation code of a call was not made for
  exactly this change (another amount, another reason, another approver's name, a made-up code), then the recipe
  shall refuse the call and write nothing.
- **R83** (complex) Where `ext-confirmed-write` is used, when `record_refund` is called with a code a person made
  for this change, the recipe shall write one row with the name the approver gave and the time in UTC.
- **R84** (complex) Where `ext-confirmed-write` is used, when a call repeats an applied request id with the same
  change, the recipe shall return `already_applied` and write no second row.
- **R85** (complex) Where `ext-confirmed-write` is used, if a call uses an applied request id for a different
  change, then the recipe shall refuse the call and leave the applied row as it is.
- **R86** (complex) Where `ext-confirmed-write` is used, if a change cannot be right (an order that does not exist,
  an amount of 0 or less or above the order's amount, a request id outside 4 to 64 characters, a reason outside 1
  to 200 characters), then the recipe shall refuse the call before a person is asked to approve it. Each refund is
  checked on its own: the refunds of one order are not added up.
- **R87** (complex) Where `ext-confirmed-write` is used, when `approve.py` shows a change and the person answers
  anything but `y`, the recipe shall print no confirmation code.
- **R88** (complex) Where `ext-confirmed-write` is used, if `APPROVAL_SECRET` is not set or is shorter than 16
  characters, then the recipe shall refuse to start the write server.
- **R89** (optional) Where `ext-confirmed-write` is used, the recipe shall keep the approval secret out of every
  tool result, log line and printed line.
- **R90** (complex) Where `ext-confirmed-write` is used, if a person's code is used more than 15 minutes after it
  was made, then the recipe shall refuse the call, say that the code has expired, and write nothing.
- **R91** (complex) Where `ext-confirmed-write` is used, when `approve.py` prints a code, the recipe shall give it
  an expiry 15 minutes ahead and say so to the person.

## Run it

> ⚠️ **Where commands run:** The extension runs on your laptop, as the main scenario does. The approver runs
> `approve.py` on their own machine. Nothing runs in AWS and no model is called (the recipe README,
> [Where each part runs](../README.md#where-each-part-runs)). There is no AWS sign-in.

1. On your laptop, in the first terminal, in `mcp-server-minimal`, set two secrets and start the write server. It prints
   `MCP server with record_refund on http://127.0.0.1:8765/mcp`; leave it running:

   ```bash
   export MCP_SERVER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
   export APPROVAL_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
   echo "$MCP_SERVER_TOKEN"; echo "$APPROVAL_SECRET"     # copy both for the second terminal
   uv run python ext-confirmed-write/write_server.py
   ```

2. In a second terminal, in `mcp-server-minimal`, set the same two values (`read` does not show a prompt: paste, then
   press Enter, once for each). Run the script to follow the flow. The script plays the model and the person:

   ```bash
   read -s MCP_SERVER_TOKEN && export MCP_SERVER_TOKEN
   read -s APPROVAL_SECRET && export APPROVAL_SECRET
   uv run python ext-confirmed-write/walk.py --approver ana
   ```

3. With an assistant, use a terminal that has `APPROVAL_SECRET` set. The person runs the command from the tool
   result to approve by hand. The command shows the change, asks `Approve this refund? [y/N]`, and prints the code after `y`:

   ```bash
   uv run python ext-confirmed-write/approve.py --request-id 2026-11-12-late-1042 --order-id 1042 \
       --amount 12.50 --reason 'arrived two days late' --approver ana
   ```

Clean-up and cost: stop the server with Ctrl-C. The run cost nothing. It wrote `output/refunds.duckdb`;
`uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` deletes it, with the sample Parquet files.

## What you should see

From `output/confirmed-write-local.txt` (a run on a laptop, 2026-11-12), shortened
:

```
{"step": "no code", "is_error": false, "result": {"status": "needs_confirmation", "changed": false, ...}}
{"step": "made-up code", "is_error": true, "result": "... confirmation code does not match this change ..."}
{"step": "code for a larger amount", "is_error": true, "result": "... confirmation code does not match this change ..."}
{"step": "with code", "is_error": false, "result": {"status": "applied", "approved_by": "ana", "changed": true, ...}}
{"step": "same call again", "is_error": false, "result": {"status": "already_applied", "changed": false, ...}}
```

Five calls result in one row in `refunds`. The code and the time change from run to run; the statuses do not. The capture
predates the expiry in the code; the five statuses are the same today.

## Adapt it

1. Replace `Refund` in `confirm.py` with your change (its fields go into the signed text), and `_validate` in
   `write_server.py` with your checks.
2. Keep one write tool per kind of change. Each one gets its own approval rule.
3. In AWS, keep `APPROVAL_SECRET` in Secrets Manager and write the row with a conditional put in DynamoDB.
4. Set `APPROVAL_TTL_S` in `confirm.py` to how long an approval may wait in your process. A retry of an applied
   call with a code that has since expired is refused as expired, not answered `already_applied`: ask for a new
   approval, and the same request id still does not write a second row.
