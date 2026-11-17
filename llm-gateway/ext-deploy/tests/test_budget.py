from concurrent.futures import ThreadPoolExecutor
from gateway import settings

import httpx
from moto.sns.models import sns_backends

import fake_upstream
from conftest import FAKE_UPSTREAM_KEY, bearer, chat_body, key_item
from gateway import notify, pricing


def _published(topic_arn, owner):
    topic = sns_backends["111122223333"]["ca-central-1"].topics[topic_arn]
    return [n for n in topic.sent_notifications if n[3]["owner"]["Value"] == owner]


def test_budget_exhausted_429_openai_shape(client, issue):
    key_id, key = issue(owner="poor", budget="0.001")
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=8192), headers=bearer(key))
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["type"] == "insufficient_quota"
    assert err["code"] == "budget_exhausted"
    assert "budget spent" in err["message"]
    assert "retry-after" not in r.headers
    assert int(key_item(key_id)["spent_micro"]) == 0


def test_max_tokens_injected_and_clipped(client, issue):
    _, key = issue()
    client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert fake_upstream.received[-1]["body"]["max_tokens"] == 4096
    client.post("/v1/chat/completions", json=chat_body(max_tokens=100_000), headers=bearer(key))
    assert fake_upstream.received[-1]["body"]["max_tokens"] == settings.DEFAULT_MAX_TOKENS_CAP
    client.post("/v1/chat/completions", json=chat_body(max_completion_tokens=99_999), headers=bearer(key))
    assert fake_upstream.received[-1]["body"]["max_completion_tokens"] == settings.DEFAULT_MAX_TOKENS_CAP


def test_overshoot_bound_with_concurrent_requests(gateway_url, issue):
    # Each reserve is 2 (input "hi" + 16) + 820 (4,096 output tokens) = 822 micro-units.
    # Three reserves fit in 3,000. The fake charges the full reserve, so a fourth never fits.
    key_id, key = issue(owner="race", budget="0.003")
    reserve = pricing.cost_in("example-large", 18) + pricing.cost_out("example-large", 4096)
    assert reserve == 822
    body = chat_body(max_tokens=4096, x_fake={"delay": 0.3, "completion_tokens": 4096})

    def call(_):
        return httpx.post(gateway_url + "/v1/chat/completions", json=body, headers=bearer(key), timeout=30)

    with ThreadPoolExecutor(8) as pool:
        codes = [r.status_code for r in pool.map(call, range(8))]
    item = key_item(key_id)
    assert codes.count(200) == 3
    assert codes.count(429) == 5
    assert int(item["spent_micro"]) == 3 * reserve <= int(item["budget_micro"])


def test_thresholds_publish_once(client, issue, topic_arn):
    # Input 4,984 bytes + 16 = 5,000 tokens = 500 micro. Output 10 tokens = 2 micro. Reserve 502.
    _, key = issue(owner="thresh", budget="0.002008")
    body = chat_body(max_tokens=10, messages=[{"role": "user", "content": "x" * 4984}],
                     x_fake={"completion_tokens": 10})
    codes = [client.post("/v1/chat/completions", json=body, headers=bearer(key)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 429]
    sent = _published(topic_arn, "thresh")
    subjects = sorted(n[2] for n in sent)
    assert subjects == [f"llm gateway: thresh at {t}% of budget" for t in (100, 50, 80)]
    # A notice names the key and its owner, never the upstream key or the upstream address.
    assert all(FAKE_UPSTREAM_KEY not in n[1] and "127.0.0.1" not in n[1] for n in sent)


def test_racing_threshold_publishes_once(issue, topic_arn):
    key_id, _ = issue(owner="racer", budget="1")
    item = key_item(key_id)
    item["spent_micro"] = 600_000
    # Two requests that both read the same item race on the conditional update.
    assert notify.check_thresholds(dict(item)) == [50]
    assert notify.check_thresholds(dict(item)) == []
    assert len(_published(topic_arn, "racer")) == 1
    assert key_item(key_id)["notified"] == {"50"}


def test_admin_key_unlimited(client, issue, topic_arn):
    key_id, key = issue(owner="prof", kind="admin")
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=8192), headers=bearer(key))
    assert r.status_code == 200
    assert "budget_micro" not in key_item(key_id)
    assert _published(topic_arn, "prof") == []


def test_refusal_sends_the_100_notice_once(client, issue, topic_arn):
    """A key refused before spent reaches 100 % still gets one 100 % notice.

    Reserve 502 micro (5,000 input tokens, 10 output); actual cost 502 too. Budget 1,300: two requests fit
    (1,004 spent, 77 %), the third's reserve does not, so it is refused at 77 % and check_thresholds alone
    would never send 100.
    """
    key_id, key = issue(owner="refused", budget="0.0013")
    body = chat_body(max_tokens=10, messages=[{"role": "user", "content": "x" * 4984}],
                     x_fake={"completion_tokens": 10})
    codes = [client.post("/v1/chat/completions", json=body, headers=bearer(key)).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    subjects = sorted(n[2] for n in _published(topic_arn, "refused"))
    assert subjects == ["llm gateway: refused at 50% of budget",
                        "llm gateway: refused budget used up (request refused)"]
    assert "100" in key_item(key_id)["notified"]

def test_tokens_set_raises_a_keys_cap(client, issue):
    key_id, key = issue()
    from types import SimpleNamespace

    from gateway import admin
    admin.cmd_tokens_set(SimpleNamespace(key_id=key_id, cap=20_000))
    client.post("/v1/chat/completions", json=chat_body(max_tokens=100_000), headers=bearer(key))
    assert fake_upstream.received[-1]["body"]["max_tokens"] == 20_000
