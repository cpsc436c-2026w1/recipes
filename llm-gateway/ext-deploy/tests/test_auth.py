import json
import secrets
import time
from types import SimpleNamespace

from conftest import bearer, chat_body, run_admin, set_attr
from gateway import auth, budget


def _err(r):
    return r.json()["error"]


def test_health(client):
    assert client.get("/health").status_code == 200


def test_missing_key_401(client):
    r = client.post("/v1/chat/completions", json=chat_body())
    assert r.status_code == 401
    assert _err(r)["code"] == "invalid_api_key"


def test_malformed_key_401(client):
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer("sk-not-ours"))
    assert r.status_code == 401


def test_unknown_key_401(client):
    key = "gw-ABCDEFGH-" + "x" * 43
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert r.status_code == 401
    assert _err(r)["message"] == "unknown API key"


def test_revoked_403(client, issue):
    key_id, key = issue()
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)).status_code == 200
    run_admin("revoke", key_id, "--reason", "test")
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert r.status_code == 403
    assert _err(r)["code"] == "key_revoked"


def test_expired_403(client, issue):
    key_id, key = issue()
    set_attr(key_id, "expires_at", 1577923199)  # 2020-01-01 end of day: gw-admin now refuses a past date
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert r.status_code == 403
    assert _err(r)["code"] == "key_expired"


def test_model_not_allowed_403(client, issue):
    _, key = issue(models="example-small")
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert r.status_code == 403
    assert _err(r)["code"] == "model_not_allowed"
    assert "example-small" in _err(r)["message"]


def test_models_lists_allowed(client, issue):
    _, key = issue(models="example-small,example-embed")
    ids = [m["id"] for m in client.get("/v1/models", headers=bearer(key)).json()["data"]]
    assert ids == ["example-embed", "example-small"]


def test_n_and_best_of_refused(client, issue):
    _, key = issue()
    assert client.post("/v1/chat/completions", json=chat_body(n=2), headers=bearer(key)).status_code == 400
    assert client.post("/v1/chat/completions", json=chat_body(best_of=2), headers=bearer(key)).status_code == 400


def test_bad_json_400(client, issue):
    _, key = issue()
    r = client.post("/v1/chat/completions", content=b"{not json", headers=bearer(key))
    assert r.status_code == 400
    assert _err(r)["code"] == "bad_json"


def test_body_too_large_413(client, issue):
    key_id, key = issue()
    set_attr(key_id, "max_body_bytes", 100)
    r = client.post("/v1/chat/completions", json=chat_body(messages=[{"role": "user", "content": "x" * 200}]),
                    headers=bearer(key))
    assert r.status_code == 413


def test_rate_limit_429_with_retry_after(client, issue, monkeypatch):
    # The limit counts requests per clock minute. Hold the gateway's clock still so the three requests
    # always land in one minute, whenever the test starts.
    now = time.time()
    monkeypatch.setattr(budget, "time", SimpleNamespace(time=lambda: now))
    key_id, key = issue()
    set_attr(key_id, "max_rpm", 2)
    codes = [client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)) for _ in range(3)]
    assert [r.status_code for r in codes[:2]] == [200, 200]
    assert codes[2].status_code == 429
    assert codes[2].headers["retry-after"] == "60"
    assert _err(codes[2])["code"] == "rate_limit_exceeded"


def test_imported_key_works_and_unknown_one_is_refused(client, tmp_path):
    token = secrets.token_urlsafe(32)  # a key another system made: 43 characters, no prefix
    f = tmp_path / "hashes.json"
    f.write_text(json.dumps({auth.hash_key(token): "t07/ana"}))
    run_admin("import", str(f))
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(token)).status_code == 200
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(secrets.token_urlsafe(32)))
    assert r.status_code == 401
    assert _err(r)["message"] == "unknown API key"


def test_key_of_the_minted_shape_is_accepted(client, tmp_path):
    token = secrets.token_urlsafe(28)  # another system's key at another length: 38 URL-safe characters
    assert len(token) == 38
    f = tmp_path / "hashes.json"
    f.write_text(json.dumps({auth.hash_key(token): "t07/bo"}))
    run_admin("import", str(f))
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(token)).status_code == 200


def test_key_of_another_length_is_refused_before_lookup(client):
    for bad in ("x" * 31, "x" * 65):
        r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(bad))
        assert r.status_code == 401
        assert _err(r)["message"] == "missing or malformed API key"
