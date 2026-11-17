# Model routing, cheap model first

Read the recipe README first. The extension "Model routing, cheap model first" builds on `main/`.

## What it changes in the main scenario

Before step 6, the router model (`qwen3.6-35b-a3b`, the cheaper chat model) labels the request `simple` or `hard`
in one word. App code maps the label to the model that answers: `simple` to `qwen3.6-35b-a3b`, `hard` to
`qwen3.8-27b`. `routing_eval.py` runs ten labelled requests under three policies: all cheap sends every request to the cheaper model, all strong sends every request to the stronger model, and routed picks a model from the router's label
and reports accuracy and cost for each, the router's own cost included.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Who decides (R81, R82) | The model labels the request. App code picks the model from the label | The mapping stays in code you can test and change; the model only answers a narrow question | AI Engineering ch. 10 (routers and intent classifiers); L17 |
| Router size (R80) | The cheaper model, reply capped at 8 tokens | The router runs on every request: it must cost much less than the call it saves. The captured gateway runs of [`model-routing`](../../model-routing/README.md) got a label under the same cap every time | [GENCOST01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost01-bp01.html) (right-size the model); [GENCOST03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp02.html) (control response length) |
| Reply cap per model (R85) | 150 tokens for the cheaper model's answer, 600 for the stronger model's | `qwen3.8-27b` reasons before it answers and the reasoning counts against `max_tokens`: with 150 its reply came back empty. 600 is the cap the [`model-routing`](../../model-routing/README.md) recipe gives the same model | Measured: `output/routing-gateway-150-cap.txt`; [GENCOST03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp02.html) (control response length) |
| Empty reply (R86) | The run reports an empty reply under the table as not scored and gives its `finish_reason`. It leaves the reply out of the accuracy | A reply with no text does not say anything about what the model knows: scored as wrong, it makes a token limit look like a weak model | Course choice, after the run of 2026-11-14 |
| Unexpected label (R81) | Anything but `simple` goes to the stronger model | A router mistake then costs money, not a wrong answer to a hard request | Course choice |
| What is measured (R83, R84) | Accuracy and cost per policy on the same requests, with a hand label per request | Routing saves money while keeping accuracy close to all-strong only if its cost is lower. The hand labels also measure the router itself | GENCOST01-BP01 (right-size the model); [GENPERF01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genperf01-bp01.html) (ground truth) |

Prices are the course LLM gateway's (0.05 and 0.10 CAD per million input and output tokens for the cheaper model,
0.10 and 0.20 for the stronger; `main/llm.py`).

## Requirements

The form table and the rules are in the [recipe README](../README.md#requirements). The scenarios are in
[`../features/routing.feature`](../features/routing.feature).

- **R80** (optional) Where routing is used, the recipe shall ask the cheaper model, `qwen3.6-35b-a3b`, for a
  one-word label of each request, with `max_tokens` 8.
- **R81** (complex) Where routing is used, if the router's reply is not `simple` (in any capitals, with at most a
  full stop around it), then the recipe shall send the request to the stronger model.
- **R82** (optional) Where routing is used, the recipe shall answer a request labelled `simple` with
  `qwen3.6-35b-a3b` and every other request with `qwen3.8-27b`.
- **R83** (optional) Where routing is used, the recipe shall report, for each of all-cheap, all-strong and routed,
  the accuracy, the cost in CAD at the gateway's prices, the router's part of that cost and the share of requests
  sent to the stronger model.
- **R84** (optional) Where routing is used, the recipe shall report the share of requests on which the router's
  label agrees with the hand label.
- **R85** (optional) Where routing is used, the recipe shall ask the cheaper model for an answer with `max_tokens`
  150 and the stronger model with `max_tokens` 600.
- **R86** (complex) Where routing is used, if an answer comes back with no text, then the recipe shall report it
  under the table as not scored, with its `finish_reason`, and leave it out of the accuracy.

## Run it

For the gateway run, set `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` as in the recipe's
[part 2](../README.md#2-laptop-with-the-course-llm-gateway). Run the commands:

```bash
uv run python ext-model-routing/routing_eval.py                    # stub replies from stub_replies.json
uv run python ext-model-routing/routing_eval.py --model gateway    # 40 calls on the course LLM gateway
```

The gateway run writes `output/routing-gateway.json` (`--out DIR` writes elsewhere). The gateway run makes 10 router calls and produces 30 answers (10 per policy). `output/routing-stub.txt` is the stub run. The
one gateway run we have, of 2026-11-14, was made when every answer was capped at 150 tokens
([`../output/routing-gateway-150-cap.txt`](../output/routing-gateway-150-cap.txt)). **Not run by us yet
(2026-11-14)** against the course LLM gateway with the stronger model's cap of 600.

## What you should see

The offline stand-in run in `output/routing-stub.txt` uses replies written by hand, so its numbers come from
stand-ins:

```
| policy | accuracy | cost (CAD) | of which router | share sent to the stronger model |
|---|---:|---:|---:|---:|
| all-cheap | 0.70 | 0.000114 | 0.000000 | 0.00 |
| all-strong | 1.00 | 0.000234 | 0.000000 | 1.00 |
| routed | 0.90 | 0.000217 | 0.000039 | 0.50 |
```

The stub replies in `stub_replies.json` are written by hand to show how the table is computed: the cheaper model's stub
misses the requests with a budget or several conditions, and the router sends one simple request to the stronger
model and one hard request to the cheaper one. The table reports accuracy and cost for the hand-written stub replies. With requests this short the router
costs a third of a cheap answer, which is why routing saves little here; it pays when answers are long or the
stronger model costs many times more. Run with `--model gateway` for the numbers to report.

**Measured with a 150 cap** (`../output/routing-gateway-150-cap.txt`, gateway run of 2026-11-14, before the
stronger model had its own cap):

```
| policy | accuracy | cost (CAD) | of which router | share sent to the stronger model |
|---|---:|---:|---:|---:|
| all-cheap | 0.80 | 0.000153 | 0.000000 | 0.00 |
| all-strong | 0.50 | 0.000550 | 0.000000 | 1.00 |
| routed | 0.60 | 0.000416 | 0.000047 | 0.50 |

router label agrees with the hand label on 1.00 of the requests
```

The router agreed with the hand label on all ten requests, and the stronger model still scored below the cheaper
one, because `qwen3.8-27b` reasons before it replies and the reasoning counts against `max_tokens`. With 150
tokens a request of this kind came back with `finish_reason` `length` and no text; with 600 it came back with an
answer (the note at the end of the capture). That run scored an empty reply as a wrong answer, so its all-strong
and routed rows measure the token limit, not the model. Do not read this table as "the stronger model is worse".

The script has changed in two ways since. The stronger model's answers are capped at 600 tokens (R85). And an
reply that does not contain text is not scored: the table's accuracy is then over the replies that contained text, and under the table
the scenario gives this example:

```
all-strong: 4 of 10 answers NOT SCORED, accuracy is over the other 6
  r01  qwen3.8-27b  empty reply, finish_reason length
```

(R86; the lines are those of the scenario, not of a gateway run). If you see such lines, raise the cap in
`main/config.py` (`ANSWER_MAX_TOKENS`), and change R85 and its scenario with it, before you compare the
policies. A larger cap costs more only when the model returns more tokens: you pay for the tokens that come back, reasoning
included.

## Adapt it

Write 20 or more of your users' requests into `requests.jsonl` with your own `simple`/`hard` label and the right
answer ids, change `ROUTER_SYSTEM` to describe your two kinds, and compare the three policies again.
