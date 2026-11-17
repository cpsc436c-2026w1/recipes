# Routing eval: label accuracy and cost per request

Read the recipe README first. This extension builds on `main/` and shows whether the router labels well enough and what routing saves.

## What it changes in the main scenario

No file in `main/` changes. `routing_eval.py` runs every request in `labelled.jsonl` twice:

1. **routed**: through `main.app.handle`, as in the main scenario. The label is compared with the hand label.
2. **all-large**: before routing. The large model answers every request with the hard path's prompt.

It prints label accuracy (overall and per hand label), a cost table for the two policies, the number of
large-model calls on the requests that are out of scope by their hand label, and each request labelled differently
from its hand label. The report
goes to `output/eval-<model>.md`; each mislabelled request also goes to `output/eval-misroutes.jsonl`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Ground truth (R50) | 24 requests labelled by hand, six per label (`labelled.jsonl`) | Accuracy needs a right answer per request. Equal counts per label keep one label's accuracy from dominating the overall accuracy and hiding another label's misses | Generative AI Lens [GENPERF01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genperf01-bp01.html) (a ground truth set of prompts and expected responses); equal counts per label are a course choice |
| Baseline (R51) | Every request on the large model, with the hard path's prompt | Compare routing with sending every request to the large model without routing | Agentic AI Lens [AGENTCOST02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp01.html) (its first anti-pattern: the largest model for all tasks) |
| Cost by path (R57) | A table of the routed policy by the path that ran: requests, cost, share of the routed cost, large-model calls | A total hides where the money goes. In the stub run the `hard` path is 8 of 24 requests and 0.78 of the cost: the next saving is there, not in the router | AGENTCOST02-BP01 (its third anti-pattern: aggregate cost that is not broken down by model tier) |
| Router cost (R55) | Counted inside the routed policy and shown on its own | A router that costs as much as it saves is not worth running | AGENTCOST02-BP01 (track cost across tiers; the pre-classifier must cost less than the price difference between the tiers) |
| Failed model call (R56) | The eval stops with the request's id and the reason; no report is written | A request the gateway refused does not have a label. If the eval counted it as a wrong label, the report would count the gateway's refusal as the router's mistake | Generative AI Lens GENREL03-BP01 (fail gracefully); course choice |
| Accuracy gate (R52) | `--min-accuracy` sets the threshold. The eval exits 1 below it | A change to the router prompt that drops accuracy fails the check before it ships | L15 (evals as tests); GENPERF01-BP01 |
| Hard cases in the set (R53, R54) | Requests that name an order number but ask for more (r17), a prompt injection (r22) | These are the requests a router gets wrong; a set of easy requests says too little | Course choice |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The requirement numbers go on from the recipe's. The scenarios are in
`features/eval.feature`.

- **R50** (optional) Where `ext-eval` is used, the recipe shall report the label accuracy on the labelled requests,
  overall and per hand label.
- **R51** (optional) Where `ext-eval` is used, the recipe shall run every labelled request under two policies,
  routed and all-large, and report the cost per request and the large-model calls of each.
- **R52** (complex) Where `ext-eval` is used, if the label accuracy is below `--min-accuracy`, then the recipe shall
  print `FAIL` with both numbers and exit with code 1.
- **R53** (complex) Where `ext-eval` is used, when a request's label differs from its hand label, the recipe shall
  list the request in the report and write one line for it to the eval's misroute log.
- **R54** (optional) Where `ext-eval` is used, the recipe shall report the large-model calls made for the requests
  whose hand label is `out_of_scope`.
- **R55** (optional) Where `ext-eval` is used, the recipe shall report the router's cost within the routed policy
  on its own.
- **R56** (complex) Where `ext-eval` is used, if a model call fails, then the recipe shall stop the eval with the
  reason and write no report.
- **R57** (optional) Where `ext-eval` is used, the recipe shall report, for each path that ran, the number of
  requests, their cost, their share of the routed cost and their large-model calls.

## Run it

Run the commands offline with the stub or with the course LLM gateway. The recipe does not create anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). From your laptop or AWS CloudShell,
in a terminal in `model-routing`:

```bash
uv run python ext-eval/routing_eval.py                          # stub labels, offline
uv run python ext-eval/routing_eval.py --min-accuracy 0.85      # exit 1 below 0.85
uv run python ext-eval/routing_eval.py --model gateway          # course LLM gateway (COURSE_GATEWAY_URL, KB_MCP_TOKEN)
```

## What you should see

**Stand-in run, offline: labels written by hand** (`output/eval-stub.md`). The labels are not model output. The real run's
first two tables follow it.

```
Label accuracy: 0.88

| hand label | requests | labelled right | router labels |
|---|---:|---:|---|
| lookup | 6 | 6 | lookup 6 |
| search | 6 | 5 | hard 1, search 5 |
| hard | 6 | 5 | hard 5, lookup 1 |
| out_of_scope | 6 | 5 | hard 1, out_of_scope 5 |

| policy | cost per request (CAD) | total (CAD) | large-model calls | of which router (CAD) |
|---|---:|---:|---:|---:|
| all-large (before routing) | 0.00004610 | 0.001106 | 24 | 0 |
| routed | 0.00002293 | 0.000550 | 8 | 0.000151 |

Routed cost is 0.50 of all-large.

Routed, by the path that ran (router call included):

| path | requests | cost (CAD) | share of routed cost | large-model calls |
|---|---:|---:|---:|---:|
| lookup | 7 | 0.000043 | 0.08 | 0 |
| search | 4 | 0.000045 | 0.08 | 0 |
| hard | 8 | 0.000430 | 0.78 | 8 |
| out_of_scope | 5 | 0.000032 | 0.06 | 0 |
Large-model calls on requests hand-labelled out_of_scope: 1
```

**Real run on the course LLM gateway, 2026-11-12** ([`output/eval-gateway.md`](../output/eval-gateway.md), its
first two tables):

```
Label accuracy: 0.92

| hand label | requests | labelled right | router labels |
|---|---:|---:|---|
| lookup | 6 | 6 | lookup 6 |
| search | 6 | 6 | search 6 |
| hard | 6 | 4 | hard 4, lookup 1, out_of_scope 1 |
| out_of_scope | 6 | 6 | out_of_scope 6 |

| policy | cost per request (CAD) | total (CAD) | large-model calls | of which router (CAD) |
|---|---:|---:|---:|---:|
| all-large (before routing) | 0.00011079 | 0.002659 | 24 | 0 |
| routed | 0.00003512 | 0.000843 | 5 | 0.000171 |

Routed cost is 0.32 of all-large.
```

The stub's labels are written by hand with three mistakes (r11, r17, r22) to show how the tables are computed.
they are not model output. The 1 in the stub report's line for large-model calls on hand-labelled out-of-scope requests is r22, the prompt injection: out of scope by hand, labelled
`hard` by the stub router, so it reached the large model. The stub model's token counts are estimates from characters, and its replies are short, so the
large model's output tokens, which a real run adds on every hard request, are mostly missing here. Run with
`--model gateway` for the numbers to report. A real run on the course LLM gateway (2026-11-12) is in
[`output/eval-gateway.md`](../output/eval-gateway.md): accuracy 0.92, routed cost 0.32 of all-large; its two
mistakes were both hand-labelled `hard` (r15 labelled `out_of_scope`, r17 labelled `lookup`)
. That capture predates the change of its last line: it reads
"requests labelled out_of_scope: 0", which counts only the requests the router itself labelled out of scope in that run.
All six out-of-scope requests were labelled right in that run, so the count by hand label is 0 as well. It also
predates the table by path, which it does not have.

Two relations to read in a real run: a label row whose misses go to `hard` costs money; a `hard` request labelled
`lookup` (r17) gets a wrong answer because the lookup path cannot answer a request for more than the order status. The router's share of the routed cost says whether
a cheaper router or a shorter system prompt is worth trying.

## What it does not measure

The eval does not measure whether each reply is right. A routed request can reach the right path and still get an incorrect reply from the small
model. To compare answer accuracy across policies, add a check per path (the expected order status, the expected
product ids), as [`hybrid-retrieval-eval/ext-model-routing`](../../hybrid-retrieval-eval/ext-model-routing/README.md) does for retrieval.

## Adapt it

1. Replace `labelled.jsonl` with 20 or more of your users' requests and your own hand labels. Add every request from
   your misroute log once you have labelled it.
2. Run with `--model gateway` and record the report with the date and the router prompt's version.
3. In your CI test step, pass your current accuracy as the value of `--min-accuracy`.
