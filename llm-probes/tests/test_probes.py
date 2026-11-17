"""Every probe against a fake endpoint, offline: the rows say yes where the feature works and no where it does not."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_endpoint  # noqa: E402

spec = importlib.util.spec_from_file_location("probe", HERE.parent / "main" / "probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

URL = "https://fake.example/v1"
CHAT_ROWS = ["chat", "streaming", "usage in the stream", "same reply on repeat", "max_tokens stops the reply",
             "tool call", "tool result used", "output fits a JSON schema", "schema wins over the prompt",
             "prompt cache reported"]


def table(good=True, **kw):
    rows, _ = probe.run(URL, fake_endpoint.KEY, transport=fake_endpoint.handler(good), **kw)
    return {(r["probe"], r["model"]): r for r in rows}, rows


def test_every_probe_says_yes_on_a_good_endpoint():
    by_name, rows = table()
    assert [r["probe"] for r in rows] == ["list models", "wrong key is refused", "unknown model is refused",
                                          *CHAT_ROWS, "embeddings"]  # the same order as output/
    assert {r["works"] for r in rows} == {"yes"}, [r for r in rows if r["works"] != "yes"]
    assert by_name[("tool call", "fake-chat")]["note"] == "run_sql('SELECT COUNT(*) FROM trips')"


def test_each_missing_feature_says_no():
    by_name, _ = table(good=False)
    for name in ["usage in the stream", "max_tokens stops the reply", "tool call", "output fits a JSON schema",
                 "schema wins over the prompt", "prompt cache reported"]:
        assert by_name[(name, "fake-chat")]["works"] == "no", name
    assert by_name[("embeddings", "fake-embed")]["works"] == "no"
    assert by_name[("chat", "fake-chat")]["works"] == "yes"  # chatting alone is not enough


def test_groups_choose_the_extensions():
    _, rows = table(groups=["main"])
    assert [r["probe"] for r in rows] == ["list models", "wrong key is refused", "unknown model is refused", "chat"]
    _, rows = table(groups=["main", "tool-calls"])
    assert [r["probe"] for r in rows][3:] == ["chat", "tool call", "tool result used"]


def test_wrong_url_or_key_stops_at_once(capsys, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_API_KEY", "some-other-key")
    assert probe.main([], transport=fake_endpoint.handler()) == 1
    out = capsys.readouterr().out
    assert "| list models |  | no |" in out and out.count("\n| ") == 2  # the header row and one probe row


def test_the_key_never_reaches_the_screen_or_the_file(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_API_KEY", fake_endpoint.KEY)
    rows_file = tmp_path / "rows.json"
    assert probe.main(["--json", str(rows_file)], transport=fake_endpoint.handler()) == 0
    out = capsys.readouterr().out
    assert fake_endpoint.KEY not in out and "[key]" in out  # the unknown-model error echoed it
    assert json.loads(rows_file.read_text())[0]["probe"] == "list models"
    assert fake_endpoint.KEY not in rows_file.read_text() and "[key]" in rows_file.read_text()


def test_a_key_that_json_escapes_is_masked_in_the_file(monkeypatch, tmp_path):
    # json.dumps writes \" for ", \\ for \ and \u00e9 for é: a mask on the encoded text would miss this key.
    key = 'k"ey\\with-é-not-real'
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_API_KEY", key)
    rows = [{"probe": "list models", "model": "", "works": "yes", "seconds": 0.1, "note": f"401 for key {key}",
             "extra": {key: [f"echo {key}"]}}]
    monkeypatch.setattr(probe, "run", lambda *a, **kw: (rows, {}))
    rows_file = tmp_path / "rows.json"
    assert probe.main(["--json", str(rows_file)]) == 0
    text = rows_file.read_text()
    assert key not in text and json.dumps(key)[1:-1] not in text
    back = json.loads(text)
    assert back[0]["note"] == "401 for key [key]" and back[0]["extra"] == {"[key]": ["echo [key]"]}
    assert key not in json.dumps(back, ensure_ascii=False)


def test_unknown_group_is_refused(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_API_KEY", fake_endpoint.KEY)
    with pytest.raises(SystemExit, match="Unknown group"):
        probe.main(["--groups", "vision"])
