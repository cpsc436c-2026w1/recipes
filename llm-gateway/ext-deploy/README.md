# Model gateway: its source, tests and deploy

Extension of the [llm-gateway recipe](../README.md). The main scenario calls the course LLM gateway; this folder is a
model gateway of the same kind: its source, its tests and its deploy into an AWS account. The course LLM gateway is
deployed from the course's own version, which can differ from this folder. Use it to read how the budget and the rate
limit behind the refusals in `main/` work, or to run a gateway of your own in front of a provider key. Nothing in
`main/` changes: a client points `COURSE_GATEWAY_URL` at your deploy's `GatewayBaseUrl` and uses a key from `gw-admin issue`.
Commands below run in this folder (`cd ext-deploy`) unless their instructions name another location.

A model gateway sits between the people who call a model and the one provider key that pays for the calls. Each
caller gets a key of their own with a budget. The gateway checks the key and the budget, forwards the request to
any OpenAI-compatible endpoint, records the tokens and the money spent per key, and publishes a notice when a key
passes 50, 80 and 100 % of its budget.

## What you need

- [uv](https://docs.astral.sh/uv/). It installs Python 3.13 or newer and the packages.
- For the tests and the local run, nothing else. They do not need an AWS account or a provider key, and after `uv sync`
  has downloaded the packages they do not need the network.
- For a deploy: an AWS account, AWS CLI v2, the AWS SAM CLI (`uv tool install aws-sam-cli` installs it), and a key
  for an OpenAI-compatible endpoint.

## Run it

### Where each part runs

"Laptop" is your own machine. "Offline" does not need a key or an AWS account. This folder is the one part of the recipe
that can run in AWS, and only if you deploy it.

| Part | Files | Laptop, offline | Laptop, against your team sandbox | In AWS, after a deploy |
|---|---|---|---|---|
| Gateway | `src/gateway/app.py`, `auth.py`, `budget.py`, `upstream.py`, `notify.py`, `textlog.py`, `run.sh` | yes: under uvicorn on `127.0.0.1`, in the tests, the scenarios and `tests/local_run.py` | no: the laptop only deploys it and calls it | yes: the Lambda function `llm-gateway`. `run.sh` starts uvicorn with `src/gateway/app.py`; the Lambda Web Adapter layer hands it each request from the function URL |
| Keys and usage | the tables `llm-gateway-keys`, `llm-gateway-usage`; with the text log, `llm-gateway-texts` | moto, which stands in for DynamoDB, SNS and Secrets Manager, inside the process | the admin tool reads and writes them | DynamoDB, on demand |
| Provider key | the secret `llm-gateway/upstream-key` | moto; a made-up key | you store it, once (step 5 of the deploy) | Secrets Manager; the function reads it |
| Notices | `src/gateway/notify.py`, the topic `llm-gateway-alerts` | moto | you subscribe your email | SNS |
| Upstream | `tests/fake_upstream.py` | yes: a second server on `127.0.0.1` | not used | your provider's endpoint, outside AWS |
| Admin tool | `src/gateway/admin.py` (`gw-admin`) | in the tests and scenarios | yes: `uv run gw-admin ...` with your sandbox credentials | never: it is not part of the function |
| Build and deploy | `infra/template.yaml`, `Makefile`, `infra/iam/*.json` | `sam build` does not need an account | `sam deploy`, from the laptop | CloudFormation creates the function, its URL, the tables and the topic |
| Tests and scenarios | `tests/`, `../features/` | yes, and in CI | never against AWS | no |

### 1. Laptop, offline: the tests

These do not need a key or an AWS account, so you do not sign in. In this folder (`cd ext-deploy` from `llm-gateway`):

```
uv sync
uv run pytest -q
```

What you should see:

```
........................................................................ [ 57%]
......................................................                   [100%]
126 passed in 17.61s
```

In the tests, [moto](https://docs.getmoto.org/) stands in for DynamoDB, SNS and Secrets Manager. A fake upstream
(`tests/fake_upstream.py`) and the gateway each run as a real server on a free localhost port, and the tests call
the gateway over HTTP. So a streamed reply in a test is a real stream. The scenarios of the
[Requirements](#requirements) run the same way, from the recipe folder: `cd .. && uv run behave`.

Clean-up and cost: nothing was spent and nothing is left.

### 2. Laptop, offline: the gateway, a fake upstream and a key

[Try it locally](#try-it-locally), below: `uv run python tests/local_run.py`. No sign-in, no cost, nothing left
behind.

### 3. In your team sandbox: the deploy

[Deploy into your own AWS sandbox](#deploy-into-your-own-aws-sandbox) using the steps below. The deploy opens with the sign-in and
`aws sts get-caller-identity`, and ends with [What it costs](#what-it-costs) and [Clean up](#clean-up).

## How a request flows

1. The request reaches the Lambda function URL. The Lambda Web Adapter hands it to uvicorn, which `run.sh` started,
   and `src/gateway/app.py` routes it. In the tests and the local run, uvicorn serves the same app directly.
2. `src/gateway/auth.py` takes the key from the `Authorization` header, hashes it with SHA-256 and reads the key's
   item from the keys table by that hash. A missing or unknown key gets 401. A revoked or expired key gets 403.
3. `app.py` checks that the body is at most 262,144 bytes and is valid JSON. It checks that the model is in the
   price table in `src/gateway/pricing.py` and that `n` equals 1. It sets `max_tokens` to 4,096 when the request has none and clips it
   to the key's cap (16,384 by default). For a stream it asks the upstream to send a usage chunk at the end.
4. `src/gateway/budget.py` counts the request in the key's current minute. With the default of 30 requests per
   minute, request 31 gets 429 with `Retry-After: 60`.
5. `budget.py` works out the highest price the request can reach: one input token per byte plus 16 per message,
   and output at `max_tokens`. It adds that amount to the key's `spent_micro` with one conditional update. The
   update goes through only when the key is active, the model is allowed for the key, and the amount still fits
   in the budget. This is the reserve.
6. When the reserve does not fit, the caller gets 429 with the code `budget_exhausted`.
   `src/gateway/notify.py` publishes the "budget used up" notice once per key.
7. `src/gateway/upstream.py` builds the upstream request: `UPSTREAM_BASE_URL` plus the path, with the provider key
   that `src/gateway/secrets.py` read from Secrets Manager. The caller's key does not go upstream and the provider
   key does not come back.
8. The reply goes back to the caller. A streamed reply is passed on chunk by chunk, unchanged, while `SSEMeter` in
   `upstream.py` reads the usage chunk so the gateway can settle the reserve at the real price.
9. `budget.settle` corrects `spent_micro` from the reserve to the real price and adds the request to the key's row
   for that day and model in the usage table. A reply that is not streamed then carries the header
   `x-gateway-cost-micro` with what the key was charged for it; a streamed reply does not, because its headers are
   sent before its price is known. When the request fails, the caller gets 502, or 429 when the upstream
   itself answered 429. A request the upstream never received (no connection) or refused (4xx) gets the whole
   reserve back. A request that was sent and then timed out or got a 5xx other than 503 or 529 keeps the whole reserve as spent
   (Decisions, "failed request").
10. `notify.py` publishes a notice once when the key passes each budget threshold of 50, 80 or 100 %.

A request that is forwarded and settled is one DynamoDB read (step 2) and four writes (steps 4 and 5, and two in
step 9); a refused request stops earlier and makes fewer.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| failed request (R64, R65, R68, R69) | When the upstream never got the request (the connection failed, or the gateway could not build it), the reserve goes back. When the upstream had it and then the gateway timed out waiting (`UPSTREAM_READ_TIMEOUT_S`, 240 s) or the upstream answered 5xx, the key is charged the whole reserve. A 4xx, a 503 (unavailable) or a 529 (overloaded) from the upstream gives the reserve back: the provider turned the request away | After the request is sent, the provider may go on generating up to `max_tokens` and may bill it, and the gateway cannot tell. Charging the reserve can charge a key more than the provider billed, never less; `gw-admin spend --reconcile` does not change it, since the usage row holds the same amount. A 2xx reply that is not streamed and does not report usage keeps the reserve too (R68). A stream that ends, stalls or breaks without a usage chunk is charged an estimate instead, the input estimate and one output token per content chunk, which can be less than the provider bills ([Known limits](#known-limits)) (R69) | Per-key budget that the gateway cannot overshoot (course constraint); the reserve is the price bound of the request (step 5) |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's; the scenarios are in
`../features/gateway.feature` and `../features/admin.feature`, and `uv run behave` in `llm-gateway` runs them
against this folder's code. A requirement says "the key's budget" and "the key's rate limit": the numbers are
settings of each key, not of the gateway.

**Keys**

- **R50** (complex) Where `ext-deploy` is used, if a request has no key, a key of another shape or a key the
  gateway does not know, then the recipe shall answer 401 with the code `invalid_api_key`.
- **R51** (complex) Where `ext-deploy` is used, if a request's key is revoked or past its last day, then the recipe
  shall answer 403 with the code `key_revoked` or `key_expired`.
- **R52** (complex) Where `ext-deploy` is used, if a request names a model outside its key's list, then the recipe
  shall answer 403 with the code `model_not_allowed`.
- **R53** (complex) Where `ext-deploy` is used, when `gw-admin issue` makes a key, the recipe shall store only the
  SHA-256 of the key and print the key once.
- **R54** (complex) Where `ext-deploy` is used, when a key asks `GET /v1/models`, the recipe shall answer with the
  key's own models, without a request to the upstream.
- **R55** (complex) Where `ext-deploy` is used, if a chat names `max_tokens` or `max_completion_tokens` above the
  key's cap, then the recipe shall forward it with the key's cap in its place.

**Budget**

- **R60** (optional) Where `ext-deploy` is used, the recipe shall reserve on the key, before it forwards a request,
  the highest price the request can reach: one input token per byte and 16 per message, and the output at
  `max_tokens`.
- **R61** (complex) Where `ext-deploy` is used, if the reserve does not fit in what is left of the key's budget,
  then the recipe shall answer 429 with the code `budget_exhausted` and not forward the request.
- **R62** (complex) Where `ext-deploy` is used, when a reply reports its usage, the recipe shall charge the request
  the price of those tokens in place of its reserve.
- **R63** (complex) Where `ext-deploy` is used, when a chat names neither `max_tokens` nor
  `max_completion_tokens`, the recipe shall forward it with `max_tokens` 4,096, or with the key's cap when that
  is lower.
- **R64** (complex) Where `ext-deploy` is used, if the upstream never received a request, or turned it away with a
  4xx, a 503 or a 529, then the recipe shall give the whole reserve back.
- **R65** (complex) Where `ext-deploy` is used, if the upstream had a request and then, before a reply started,
  answered another 5xx or did not answer in time, then the recipe shall keep the whole reserve as spent.
- **R66** (optional) Where `ext-deploy` is used, the recipe shall count every settled request in the usage row of
  its key, its UTC day and its model.
- **R67** (complex) Where `ext-deploy` is used, if the gateway cannot bound a request (a body that is not a JSON
  object, a body over the key's size limit, `n` other than 1, `best_of`), then the recipe shall refuse it with 400
  or 413 before any reserve.
- **R68** (complex) Where `ext-deploy` is used, if a reply that is not streamed has a 2xx status and reports no
  usage, then the recipe shall keep the whole reserve as spent and count the request as estimated.
- **R69** (complex) Where `ext-deploy` is used, if a streamed reply ends, stalls or breaks without a usage chunk,
  then the recipe shall charge the request the input estimate of its reserve and one output token for each
  content chunk it received, and count the request as estimated.

**Rate**

- **R70** (complex) Where `ext-deploy` is used, if a key sends more requests in one clock minute than the key's
  rate limit, then the recipe shall answer 429 with the code `rate_limit_exceeded` and `Retry-After: 60`.
- **R71** (complex) Where `ext-deploy` is used, when a request is refused, the recipe shall leave the key's
  spend as it was.

**Notices**

- **R80** (complex) Where `ext-deploy` is used, when a key's spend passes 50, 80 or 100 % of the key's budget, the
  recipe shall publish one notice for that threshold.
- **R81** (complex) Where `ext-deploy` is used, when a request is refused on budget, the recipe shall publish the
  "budget used up" notice, once per key.

**Upstream and log**

- **R90** (optional) Where `ext-deploy` is used, the recipe shall send the provider key to the upstream only, and
  the caller's key to nobody.
- **R91** (optional) Where `ext-deploy` is used, the recipe shall pass a streamed reply on unchanged, and ask the
  upstream for the usage in the stream.
- **R92** (complex) Where `ext-deploy` is used, if the upstream answers 429, then the recipe shall answer 429 with
  the code `upstream_rate_limited` and the upstream's `Retry-After`.
- **R93** (complex) Where `ext-deploy` is used, if the upstream answers 5xx or does not answer in time, then the
  recipe shall answer 502 with the code `upstream_error`, without the upstream's own message.
- **R94** (optional) Where `ext-deploy` is used, the recipe shall write one log line per request with the key id,
  and without the key, its owner or the messages.
- **R95** (complex) Where `ext-deploy` is used, when a reply that is not streamed has been settled from its usage,
  the recipe shall name what the key was charged, in micro-units, in the reply header `x-gateway-cost-micro`.
- **R96** (complex) Where `ext-deploy` is used, if the upstream answers a 4xx other than 401, 403 or 429, then the
  recipe shall answer 400 with the code `upstream_rejected` and the provider's own error message, cut to 300
  characters, at no charge to the key.
- **R97** (complex) Where `ext-deploy` is used, if the upstream answers 401 or 403, then the recipe shall answer
  502 with the code `upstream_key_refused`, at no charge to the key.

**Text log**

- **R100** (optional) Where `ext-deploy` is used without the text log, the recipe shall store no request text and
  no reply text.
- **R101** (complex) Where `ext-deploy` is used with the text log, when a request is forwarded, the recipe shall
  store one item with the request as it went upstream and the reply.

**`gw-admin`**

- **R110** (complex) Where `ext-deploy` is used, when `gw-admin revoke` names a key, the recipe shall refuse that
  key's next request with 403.
- **R111** (complex) Where `ext-deploy` is used, when `gw-admin spend` runs, the recipe shall print, per key, day
  and model, the requests, the tokens and the amount, and a total per key.
- **R112** (complex) Where `ext-deploy` is used, when `gw-admin budget set` raises a key's budget above its spend,
  the recipe shall forward the key's requests again.
- **R113** (complex) Where `ext-deploy` is used, when `gw-admin import` is given the SHA-256 of a key that another
  system made, the recipe shall accept that key, without having seen it.
- **R114** (complex) Where `ext-deploy` is used, when `gw-admin kill` runs, the recipe shall set the function's
  reserved concurrency to 0.
- **R115** (complex) Where `ext-deploy` is used, when `gw-admin open` runs, the recipe shall set the function's
  reserved concurrency to 200.

**Deploy and clean-up**

- **R120** (optional) Where `ext-deploy` is used, the recipe shall create no IAM role and no IAM policy in its
  template.
- **R121** (optional) Where `ext-deploy` is used, the recipe shall name, in its clean-up commands, what the stack
  holds and what the deploy steps made beside it: the stack, the three tables, the secret, the function's role
  with its inline policy, and the inline policy `llm-gateway-deploy` on the role you work with.

## Four rungs

Start with Pass-through in the table. Read the code or build your own one rung at a time; each rung adds one thing
to the rung before it.

| Component | What it adds | Files | Tests for this component |
|---|---|---|---|
| Pass-through | One URL in front of the upstream. The provider key stays in Secrets Manager, and replies stream back unchanged. | `src/gateway/app.py`, `upstream.py`, `secrets.py`, `run.sh`, `infra/template.yaml` | `uv run pytest -q tests/test_streaming.py tests/test_secrets.py` |
| Keys | A key per caller, stored as a hash. Issue, revoke, expiry, allowed models. | `src/gateway/auth.py`, `admin.py` (`gw-admin issue`, `import`, `revoke`, `list`), the keys table | `uv run pytest -q tests/test_auth.py tests/test_admin.py` |
| Budget | A price table, a reserve before the call, a settle after it, a rate limit, usage rows per day and model, a spend report. | `src/gateway/pricing.py`, `budget.py`, `admin.py` (`gw-admin spend`, `budget set`, `rate set`), the usage table | `uv run pytest -q tests/test_budget.py tests/test_usage.py` |
| Alerts | SNS notices at 50, 80 and 100 % and on the first refusal, an email subscription per owner, a switch that closes the URL. | `src/gateway/notify.py`, `admin.py` (`gw-admin notify`, `kill`, `open`), the topic | `uv run pytest -q tests/test_budget.py -k "threshold or notice"` |

`tests/test_edge_cases.py` holds input checks that do not belong to a single component, and two checks that read the template
and the IAM policies as text.

## Try it locally

```
uv run python tests/local_run.py
```

The script starts the gateway, the fake upstream and moto in one process. It issues a key with a budget of
0.001 USD and sends six requests. Each request asks for up to 1,000 output tokens on `example-large`, so its reserve
is 0.000202 USD. Four reserves fit in the budget. The fifth does not, and the key is refused.

```
gateway  http://127.0.0.1:53653/v1  (fake upstream and fake AWS in this process)
key      gw-4XNU6YWI  owner local  budget 0.001000 USD

request 1  HTTP 200  spent 0.000202 USD
request 2  HTTP 200  spent 0.000404 USD
request 3  HTTP 200  spent 0.000606 USD
request 4  HTTP 200  spent 0.000808 USD
request 5  HTTP 429  spent 0.000808 USD
           budget_exhausted: budget spent: 0.000808 of 0.001000 USD used. Ask the person who issued your key for more.
request 6  HTTP 429  spent 0.000808 USD
           budget_exhausted: budget spent: 0.000808 of 0.001000 USD used. Ask the person who issued your key for more.

notices published to the SNS topic
  llm gateway: local at 50% of budget
  llm gateway: local at 80% of budget
  llm gateway: local budget used up (request refused)

gw-admin spend
  gw-4XNU6YWI  2026-11-11  example-large  requests 4  in 72  out 4000  0.000808 USD
  total gw-4XNU6YWI  0.000808 USD

gw-admin list
  gw-4XNU6YWI  local  person  -  0.000808 / 0.001000 USD  2027-05-10  active
```

Your port, key id and day will differ. Three things to read in this output:

- The key is refused at 0.000808 USD, before `spent` reaches the budget. A request starts only when its highest
  possible price still fits in what is left.
- The second refusal does not publish a second notice.
- The day in `gw-admin spend` is the UTC day.

To call the gateway yourself, keep it running:

```
uv run python tests/local_run.py --serve
```

It prints a base URL, a test key and a `curl` command. Open a second terminal on your laptop and paste the
command there. You should see
three `data:` chunks with content, one chunk with `usage`, and `data: [DONE]`. Ctrl-C stops it, and the key stops
working when the process ends.

## Set your upstream, models and prices

1. Set the upstream endpoint in the environment variable `UPSTREAM_BASE_URL`. The gateway reads it as the base URL
   with its version path, for example `https://api.example.com/v1`. The gateway adds `/chat/completions` and
   `/embeddings` to it. It does not have a default, and without it every request gets 502. In a deploy you set it with the
   template parameter `UpstreamBaseUrl`. `infra/parameters.example.json` lists the four template parameters with
   example values.
2. In `src/gateway/pricing.py`, edit `PRICES`, `CHAT_MODELS` and `EMBEDDING_MODELS`. The ids
   are the names callers send in `model` and the names the gateway passes upstream. A price is in micro-units per
   million tokens, so `100_000` is 0.10 per million tokens. The three rows in the file are examples. The tests
   use them, so when you replace the rows, change the ids and the expected amounts in `tests/` as well.
3. **Currency.** Money is counted in whole micro-units: 1,000,000 micro-units are one unit of your currency. The
   label printed next to every amount is `CURRENCY` in `src/gateway/settings.py` (`USD` as shipped). Put your prices
   in that currency and change the label. The arithmetic stays the same. The tests expect `USD`.
4. **Defaults for a new key.** `src/gateway/settings.py` holds them: the budget per kind of key (`person` 3,
   `team` 20, `admin` no budget), the days until a new key expires (180), 30 requests per minute, the `max_tokens`
   cap of 16,384, the body limit, and `KEY_PREFIX`, the first part of every key id. Rename the kinds there and in
   `admin.py`. The refusal text "Ask the person who issued your key for more." is in `budget.py`.

The gateway reads these environment variables. The template sets the first eight.

| Variable | Default | What it sets |
|---|---|---|
| `UPSTREAM_BASE_URL` | none, required | the OpenAI-compatible endpoint |
| `SECRET_ARN` | `llm-gateway/upstream-key` | the secret that holds the provider key, by name or ARN |
| `TABLE_KEYS` | `llm-gateway-keys` | the keys table |
| `TABLE_USAGE` | `llm-gateway-usage` | the usage table |
| `TABLE_TEXTS` | empty | the table of the text log. When it is empty, the gateway does not store any text |
| `TEXT_TTL_DAYS` | 180 | days before DynamoDB deletes a stored text. 0 keeps it until you delete it |
| `TOPIC_ARN` | empty | the SNS topic for notices. When it is empty, a notice is logged and not published |
| `FUNCTION_NAME` | `llm-gateway` | the function that `gw-admin kill` and `open` change |
| `AWS_REGION` | `ca-central-1` | the region of the tables, the topic and the secret |
| `SECRET_TTL_S` | 900 | seconds before the provider key is read from Secrets Manager again |
| `DISCONNECT_GRACE_S` | 5 | seconds the gateway keeps reading the upstream after the caller disconnects |
| `UPSTREAM_CONNECT_TIMEOUT_S` | 10 | seconds to connect to the upstream |
| `UPSTREAM_READ_TIMEOUT_S` | 240 | seconds to wait for the next bytes from the upstream; a thinking model can take minutes before its first byte |
| `UPSTREAM_TOTAL_TIMEOUT_S` | 285 | seconds for one whole request, under the function's timeout of 300 |

### How upstream errors are reported

| Upstream answer | Caller receives | Key charge |
|---|---|---|
| 401 or 403 (the gateway's own provider key) | 502 `upstream_key_refused`, a plain message; the log line `upstream refused the provider key` | Nothing |
| 429 | 429 `upstream_rate_limited`, with the upstream's `Retry-After` when it sent one | Nothing |
| Any other 4xx (a parameter the model does not take, say `temperature` 0) | 400 `invalid_request_error`, code `upstream_rejected`, with the provider's own message cut to 300 characters | Nothing |
| 503 or 529 | 502 `upstream_error` | Nothing |
| Another 5xx, or a timeout after sending | 502 `upstream_error` | The whole reserve |

Only the provider's message string is passed on (`error.message`, or `error`, or `message`), never the body. A body
without one gets "the model service rejected the request (HTTP 400)". A 400 `upstream_rejected` is not worth
resending: change the request. Tests: `tests/test_upstream_errors.py`.

**A request that times out after it was sent is charged its whole reserve.** This is intended (`budget.py`): once
the provider has the request it may generate and bill up to `max_tokens`, and the gateway cannot tell how much it
did. Keep `max_tokens` close to what the reply needs, and give a thinking model the 240 seconds above.

## Deploy into your own AWS sandbox

**Not run by us from this folder yet (2026-11-13).** The course's live gateway was deployed with these steps from a
separate copy of this code, outside this repository. A change in this folder does not change it. The course LLM
gateway sets its own limits on each key (the key's budget and the key's rate limit); the defaults in this folder
are not those. The copy here is checked by its offline tests and scenarios, and by tests that read the template
and the policies as text.

Sign in to your team sandbox first, and check that the account is the one you expect:

```
aws sso login                                          # or the sign-in your team uses for its sandbox
aws sts get-caller-identity                            # the account you expect, before anything is created
```

Before you deploy, have an OpenAI-compatible provider you can call: its base URL, the model ids, their prices and
a key. The fake upstream of the local run is for tests only. It is not a provider for a deploy.

The template creates one Lambda function with a public function URL, two DynamoDB tables, one SNS topic and one
log group. With the parameter `TextLog=On` it creates a third table
([Store what was asked and what came back](#store-what-was-asked-and-what-came-back)). This list covers the template:
the deploy steps create resources separately, and SAM may create a bucket. The template does not create anything in
IAM, so you make the role first.

The function URL gets two permissions in the template, `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`.
With only the first one the URL answers 403
([Control access to Lambda function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html)).

**AWS permissions.** The deploy and `gw-admin` need the permissions in `infra/iam/deploy-admin-policy.json`, and
the buffered transport also needs `infra/iam/deploy-buffered-addon-policy.json` (step 4 below says who adds them).
The steps outside the stack also need these IAM actions:

| Step | IAM actions |
|---|---|
| Account check and function role (steps 2 and 3) | `lambda:GetAccountSettings`, `iam:CreateRole`, `iam:PutRolePolicy` |
| Provider key (step 5) | `secretsmanager:CreateSecret` |
| Clean-up | `iam:DeleteRolePolicy`, `iam:DeleteRole`, `secretsmanager:DeleteSecret`, `dynamodb:DeleteTable` |

1. Check that `aws sts get-caller-identity` shows the account you mean to deploy into. Then put your account id and
   region into the policy files. They ship with the placeholder account `111122223333` and the region
   `ca-central-1`, and the command below edits them in place.

   ```
   ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
   REGION=ca-central-1
   sed -i "s/111122223333/$ACCOUNT/g; s/ca-central-1/$REGION/g" infra/iam/*.json
   ```

   On macOS write `sed -i ''` in place of `sed -i`.

2. Check the account's Lambda concurrency quota.

   ```
   aws lambda get-account-settings --region $REGION
   ```

   The template reserves 200 concurrent executions for the function, and Lambda always keeps 100 unreserved
   ([Concurrency quotas](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html))
. So
   `UnreservedConcurrentExecutions` in the answer must be 300 or more: it is what is left of
   `ConcurrentExecutions` after the reservations other functions in the account already hold. When it is lower,
   lower `ReservedConcurrentExecutions` in `infra/template.yaml`.

3. Create the function's role. Its policy lets the function read and update items in the tables, read the one
   secret, publish to the one topic and write its own log.

   ```
   aws iam create-role --role-name llm-gateway-fn \
     --assume-role-policy-document file://infra/iam/function-role-trust.json
   aws iam put-role-policy --role-name llm-gateway-fn --policy-name llm-gateway-fn \
     --policy-document file://infra/iam/function-role-policy.json
   ```

4. When the role you work with is not an administrator, ask an administrator of the account to add
   `infra/iam/deploy-admin-policy.json` to that role as an inline policy (a role without IAM permissions cannot add
   it to itself). It holds the permissions for the deploy and for `gw-admin`, limited to this stack's resources. It
   also denies creating roles and policies, so do this after step 3. The administrator runs, with your role's name
   in place of the angle brackets:

   ```
   aws iam put-role-policy --role-name <the role you work with> --policy-name llm-gateway-deploy \
     --policy-document file://infra/iam/deploy-admin-policy.json
   ```

5. Store the provider key as a plain string in Secrets Manager. `read -s` keeps it out of your shell history; the
   key is still an argument of the `aws` command while it runs, so do this on a machine only you use.

   ```
   read -s UPSTREAM_KEY
   aws secretsmanager create-secret --region $REGION --name llm-gateway/upstream-key \
     --secret-string "$UPSTREAM_KEY"
   unset UPSTREAM_KEY
   ```

   Note the `ARN` in the reply.

6. In the AWS Billing and Cost Management console of the same account, create a cost budget in AWS Budgets with an
   email alert, and confirm the email. The gateway limits what callers spend at the provider. It does not limit
   your AWS bill.

7. Build and deploy. `sam build` runs the `Makefile`, which needs `uv` on the path.

   ```
   cd infra
   sam build
   sam deploy --stack-name llm-gateway --region $REGION --resolve-s3 --parameter-overrides \
     ParameterKey=FunctionRoleArn,ParameterValue=arn:aws:iam::$ACCOUNT:role/llm-gateway-fn \
     ParameterKey=SecretArn,ParameterValue=<the secret's ARN> \
     ParameterKey=UpstreamBaseUrl,ParameterValue=<your provider's base URL, ending in its version path such as /v1>
   cd ..
   ```

   `--resolve-s3` lets SAM make an S3 bucket for the build, in a second stack named `aws-sam-cli-managed-default`
.
   The stack outputs include `GatewayBaseUrl` and `AlertsTopicArn`. Check that the function answers. The health
   path is beside `v1`, not under it:

   ```
   curl https://<the host in GatewayBaseUrl>/health
   ```

   What you should see is `{"status":"ok"}`. The first call takes a second or two while Lambda starts the
   function.

8. Subscribe your email to the notices, and confirm the email that SNS sends you.

   ```
   aws sns subscribe --region $REGION --topic-arn <AlertsTopicArn> --protocol email \
     --notification-endpoint you@example.com
   ```

9. Issue a key and call the gateway. `gw-admin` runs with your own AWS credentials and talks to DynamoDB, SNS and
   Lambda directly. The key is printed once and only its hash is stored.

   ```
   export AWS_REGION=$REGION TOPIC_ARN=<AlertsTopicArn>
   uv run gw-admin issue --owner ana --kind person --budget 1
   curl -N <GatewayBaseUrl>/chat/completions -H "Authorization: Bearer <the key>" \
     -H "Content-Type: application/json" \
     -d '{"model": "<a model id from your price table>", "stream": true, "messages": [{"role": "user", "content": "hi"}]}'
   ```

   A caller who uses an OpenAI client sets its base URL to `GatewayBaseUrl` and its API key to the gateway key.

Select the other `gw-admin` commands you need:

```
uv run gw-admin list
uv run gw-admin spend --by key --since 2026-11-01
uv run gw-admin budget set gw-ABCDEFGH 1
uv run gw-admin rate set gw-ABCDEFGH 60      # requests a minute; `all` in place of the id changes every active key
uv run gw-admin revoke gw-ABCDEFGH --reason "key was shared"
uv run gw-admin notify --owner ana --email ana@example.com
uv run gw-admin kill
uv run gw-admin open
uv run gw-admin dump backup.json             # both tables to one file, mode 600; it holds key hashes and owners
uv run gw-admin load backup.json             # into empty tables; --overwrite when the keys table holds keys
```

A new key gets 30 requests a minute. An optimizer or an eval that runs calls in parallel needs more:
`gw-admin rate set KEY 120`, or `--rpm 120` on `issue` or `import`.

A request for more than the key's `max_tokens_cap` (16,384 as shipped) is clamped to it without an error, so a
client that asks for 32,768 gets at most 16,384. Set the client's own cap at or under the key's.

`notify` emails one owner the notices for their own keys. `kill` sets the function's reserved concurrency to 0, so
the URL answers every request with 429
([Deactivating function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html))
. `open` sets it back to 200. `dump` and `load` are the backup: spend lives only in the two tables, so a copy outside
the account is what lets a new deployment continue from the same budgets.

### Keys that your callers already hold

If another system of yours already gave each caller a key, the gateway can accept that same key. `gw-admin import`
reads a JSON file of the form `{SHA-256 hex of a key: owner}` and stores one gateway key per line. The gateway does
not see the keys themselves, at import or later: it stores the same hash that it stores for a key of its own.

```
uv run gw-admin import hashes.json --team-sep / --budget 1 --expires 2026-12-31
uv run gw-admin import hashes.json --revoke-missing
```

- A key must be 32 to 64 URL-safe characters (`secrets.token_urlsafe(24)` to `(48)`). To accept another shape,
  change `IMPORTED_KEY_RE` in `src/gateway/auth.py`.
- The id of an imported key is `im-` and 8 characters taken from its hash, so one key has one id in every import.
  `list`, `spend`, `budget set`, `rate set` and `revoke` take that id.
- With `--team-sep /`, an owner written `t07/ana` gets the team `t07`, and `gw-admin spend --by team` adds it up.
- A second import of the same file adds only the hashes that are new. A key that is already stored keeps its
  budget and its spend.
- `--revoke-missing` revokes each imported key whose hash is no longer in the file. Run it after the other system
  takes a key away, so that the key stops working in both places.
- The command prints counts and does not print an owner or a hash.

One key now opens two services, and here it also spends money. A caller who leaks it leaks both, so keep the budget
per key small.

To put a REST API in front of the function in place of the function URL, add
`infra/iam/deploy-buffered-addon-policy.json` to your role before you deploy with the parameter
`Transport=ApiGatewayBuffered`. That path does
not stream, and API Gateway ends a request after 29 seconds by default. The limit can be raised, which may lower
the account's throttle quota in that Region
([Quotas for a REST API](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-execution-service-limits-table.html))
.

### What it logs

The function writes one line per request to its CloudWatch log group, `/aws/lambda/llm-gateway`. A line carries
the key id and never the key, its owner or the messages.

```text
INFO request_id=<id> key_id=gw-ABCDEFGH model=example-chat status=200 tokens_in=41 tokens_out=120 cost_micro=85 estimated=False ms=2310
INFO refused request_id=<id> key_id=gw-ABCDEFGH status=429 code=rate_limit_exceeded
INFO refused request_id=<id> key_id=- status=401 code=invalid_api_key
WARNING upstream error request_id=<id> error=ReadTimeout charged=True
```

`ms` is the time from the reserve to the settle. `charged=True` on an upstream error means the request had been
sent, so the whole reserve stays as spent; `charged=False` means it never reached the upstream and the reserve went
back. A refusal with `key_id=-` came before the key was known. Read the
last ten minutes on your laptop with `aws logs tail /aws/lambda/llm-gateway --since 10m`.

### Store what was asked and what came back

A log line records how much a request used without recording what the request asked. To keep the text as well, turn on
the text log. It is off by default, and while it is off the gateway does not store any text. The provider still
receives every request the gateway forwards and keeps it under the provider's own terms.

The text log is the callers' own words. Tell your callers that you keep them, for how long, and who can read them,
before you turn it on.

1. Check that the function's role allows `dynamodb:UpdateItem` on the table `llm-gateway-texts`.
   `infra/iam/function-role-policy.json` lists the table. A role made from an older copy of that file needs
   `put-role-policy` again (step 3 of the deploy).
2. Deploy again with one more parameter, `ParameterKey=TextLog,ParameterValue=On`. The stack creates the table
   `llm-gateway-texts` and sets `TABLE_TEXTS` on the function. `TextLogDays` sets how many days a text is kept
   (180 by default, 0 keeps it until you delete it).
3. Send one chat request and note the `x-request-id` header of the reply.
4. Read the item for that request. Its key is the key id and the request id, the two values on every log line:

   ```
   aws dynamodb get-item --region $REGION --table-name llm-gateway-texts \
     --key '{"key_id": {"S": "gw-ABCDEFGH"}, "sk": {"S": "<the request id>"}}'
   ```

The gateway writes one item for each request that it forwards to the upstream.

| Attribute | What it holds |
|---|---|
| `key_id`, `sk` | the item's key: the key id and the request id |
| `request_id`, `team`, `model`, `endpoint`, `status`, `ts` | the request id again, the key's team label (`-` without one), the model, `/chat/completions` or `/embeddings`, the upstream's status, the time in UTC |
| `source` | `gateway`. Another program that writes its own text to the same table uses another value |
| `request` | the JSON body as the gateway sent it to the upstream: the messages, the tools and the parameters |
| `reply` | the first choice of a chat reply as JSON: the content, the reasoning text, the tool calls and the finish reason. A streamed reply is put back together from its chunks. Embeddings and a failed upstream call do not have it |
| `truncated` | only on a request too long for one item: the size of the whole request in bytes. The item then holds the start and the end of the request |
| `ttl` | the time when DynamoDB deletes the item |

A request that the gateway refuses (a bad key, a spent budget, a model outside the key's list) is not forwarded, so
it does not have an item. A DynamoDB item holds at most 400 KB
([Constraints in DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html))
,
which is why a very long request is cut in the middle. When the write fails, the caller still gets the reply, and
the log gets the line `text log failed request_id=<id> key_id=<id> error=<type>`. The text itself does not go to
any log line.

`gw-admin texts` copies items to a file on your machine and prints only counts. Set `TABLE_TEXTS` to the stack
output `TextsTableName` first.

```
export TABLE_TEXTS=llm-gateway-texts
uv run gw-admin texts texts.jsonl --team t1 --since 2026-11-01   # every key with the team label t1
uv run gw-admin texts one.jsonl --key gw-ABCDEFGH --request <the request id>
```

The file has one JSON object per line and mode 600. Without `--key` or `--team` the command reads the whole table.

DynamoDB bills a write by the size of the item, one write request unit for each kilobyte
([On-demand capacity mode](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html)),
so a request with a long conversation in it uses more units than a short one. `gw-admin dump` does not include
this table. The table has point-in-time recovery on.

The text log is covered by `tests/test_textlog.py`. It has not been run in a deployed stack yet.

### What it costs

A Secrets Manager secret is 0.40 USD a month. Lambda, DynamoDB and SNS bill per request. An estimate for an assumed
workload of 20,000 chat requests of 10 seconds each and 5,000 embedding requests a month in
`ca-central-1` (AWS bulk price lists; in USD):

| What is billed | How much of it | Price | A month |
|---|---|---|---|
| Lambda duration, ARM, 512 MB | 20,000 x 10 s x 0.5 GB = 100,000 GB-seconds | 0.0000133334 per GB-second | 1.33 |
| Lambda requests | 25,000 | 0.20 per million | 0.01 |
| DynamoDB writes, on demand | 4 per request: 100,000 | 0.6875 per million | 0.07 |
| DynamoDB reads, on demand | 1 per request: 25,000 | 0.1375 per million | under 0.01 |
| Secrets Manager | 1 secret | 0.40 per secret a month | 0.40 |
| SNS | a few notices | the first 1,000 emails a month are free | 0 |


That is about 1.8 USD a month for the gateway, or about 0.50 USD while the
account's Lambda free tier covers the duration. The tokens are billed by your provider on
top of that.

### Clean up

These commands delete the stack, the secret, the function's role, the tables, and the inline policy that step 4
put on the role you work with (an administrator runs that line, as in step 4; leave it out when step 4 was not
needed). The template does not create a role: the function's role is the one you made in step 3. If you need the keys
and spend history, run `uv run gw-admin dump backup.json` first. Deleting the tables removes every key and the spend
history for good.
A table you never created (the text table without the text log) answers `ResourceNotFoundException`, which you
can ignore.

```
sam delete --stack-name llm-gateway --region $REGION
aws dynamodb delete-table --region $REGION --table-name llm-gateway-keys
aws dynamodb delete-table --region $REGION --table-name llm-gateway-usage
aws dynamodb delete-table --region $REGION --table-name llm-gateway-texts   # when you turned the text log on
aws secretsmanager delete-secret --region $REGION --secret-id llm-gateway/upstream-key \
  --force-delete-without-recovery
aws iam delete-role-policy --role-name llm-gateway-fn --policy-name llm-gateway-fn
aws iam delete-role --role-name llm-gateway-fn
aws iam delete-role-policy --role-name <the role you work with> --policy-name llm-gateway-deploy
```

The tables have `DeletionPolicy: Retain`, so `sam delete` keeps them and the spend history in them. The text
table also stays when you deploy again with `TextLog=Off`. Delete it before you turn the text log on again:
the deploy fails while a table with that name exists. That is
why they are deleted by hand here. The secret is deleted with `--force-delete-without-recovery`: without it Secrets
Manager keeps the secret for a recovery window of 30 days, and step 5 of a new deploy fails in that time because the
name is still taken
([DeleteSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DeleteSecret.html))
.
Even with it the deletion runs in the background: if a new `create-secret` with the same name fails right after,
wait a minute and run it again. A second `delete-secret` on a secret that is already gone is not an error. The stack
`aws-sam-cli-managed-default` and its bucket stay for your next SAM deploy.

## Known limits

- **A stream that is cut is undercounted.** When the caller disconnects, the gateway keeps reading the upstream
  for 5 seconds (`DISCONNECT_GRACE_S`). When the upstream finishes in that time, its usage chunk arrives and the
  count is exact. When it does not, the gateway stops reading and settles on an estimate: the input estimate plus
  one output token per content chunk it has seen. The upstream may go on generating after that, and one chunk can
  hold more than one token, so the key's `spent` can be lower than what the provider bills for that request. The
  same estimate is used when the upstream breaks in the middle of a stream. These requests are counted in
  `requests_estimated` in the usage row. To charge the whole reserve in these cases, call
  `req.finish_at_reserve()` in the last branch of `_read_upstream` in `app.py`.
- **The estimate counts content chunks only.** A model that streams its reasoning in another field does not add anything
  to the estimate.
- **An upstream that does not send a usage chunk is always estimated.** The gateway asks for one with
  `stream_options.include_usage`. Check that your upstream honours it.
- **A function that Lambda stops at its timeout does not settle.** The reserve stays in `spent`, so the key is
  charged more than it used. `gw-admin spend --reconcile` sets `spent` back to the sum of the usage rows. When no
  request is in flight, run it, because it also removes open reserves.
- **The reserve is an upper bound.** A key is refused when the highest possible price of the next request does
  not fit, which is before `spent` reaches the budget. The local run above shows it.
- **The rate limit counts per clock minute.** A caller can send 30 requests at the end of one minute and 30 more
  at the start of the next.
- **Calls to DynamoDB, SNS and Secrets Manager block the event loop.** In Lambda one execution environment serves
  one request at a time, so nothing waits. On a long-running server with many requests at once they would wait
  for each other.
- **Only `/v1/chat/completions` and `/v1/embeddings`.** There is no `/v1/responses`. A client that defaults to the
  Responses API (DSPy 3.5 with `model_type` "auto" on an `openai/` model) gets 404: set it to chat.
- **Each request opens its own connection to the upstream.**
- **`gw-admin list` and `gw-admin spend` scan the tables.** A scan reads every item, so both commands get slower
  as the tables grow.
- **The function URL is public.** Anyone who has the URL can send requests, and the key check is what stops them.
  `gw-admin kill` closes the URL.
- **The tests run the app under uvicorn, without Lambda and without the Lambda Web Adapter.** They do not show
  what the adapter does when a caller disconnects in the middle of a stream.
- **`infra/apigw-stream-openapi.yaml` is not used by the template.** It is a sketch of a streaming API Gateway
  path.

## Ideas to extend

- **Fallback model.** When the first model times out, send the same request to a second model. This is the
  smallest router. Start in `_buffered` in `app.py`. Decide which price the reserve uses when the two models have
  different prices, and record in the usage row which model answered.
- **Budget per team.** `gw-admin issue --kind team` already makes one key that a team shares, and `--team` puts
  a label on a key so that `gw-admin spend --by team` adds up its members. The step to build is a budget that the
  members' own keys share: a team item in the keys table, and a reserve that updates the key and the team in one
  DynamoDB transaction.
- **Cache.** Store the reply to a request that is not streamed under a hash of its model, messages and
  parameters, in a DynamoDB item with a TTL. Decide whether a reply from the cache is charged to the key.
