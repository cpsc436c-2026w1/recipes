import logging

from conftest import bearer, chat_body, set_attr
from gateway import app as gateway_app


def lines(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.name == "gateway"]


def test_gateway_logger_has_a_handler_at_info():
    log = logging.getLogger("gateway")
    assert log.level == logging.INFO
    assert gateway_app._handler in log.handlers  # without one, Python prints only WARNING and above


def test_settled_request_logs_its_duration(client, issue, caplog):
    caplog.set_level(logging.INFO)
    key_id, key = issue()
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)).status_code == 200
    line = next(x for x in lines(caplog) if x.startswith("request_id=") and f"key_id={key_id}" in x)
    assert " status=200 " in line
    assert int(line.rsplit("ms=", 1)[1]) >= 0


def test_refusal_is_logged_with_key_id_and_code(client, issue, caplog):
    caplog.set_level(logging.INFO)
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(n=3), headers=bearer(key))
    assert r.status_code == 400
    rid = r.headers["x-request-id"]
    assert f"refused request_id={rid} key_id={key_id} status=400 code=n_not_supported" in lines(caplog)


def test_refusal_of_a_revoked_key_on_models_is_logged(client, issue, caplog):
    caplog.set_level(logging.INFO)
    key_id, key = issue()
    set_attr(key_id, "state", "revoked")
    assert client.get("/v1/models", headers=bearer(key)).status_code == 403
    assert any(f"key_id={key_id} status=403 code=key_revoked" in x for x in lines(caplog))


def test_unknown_key_is_logged_without_the_key(client, caplog):
    caplog.set_level(logging.INFO)
    key = "A" * 43
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)).status_code == 401
    logged = "\n".join(lines(caplog))
    assert "key_id=- status=401 code=invalid_api_key" in logged
    assert key not in logged
