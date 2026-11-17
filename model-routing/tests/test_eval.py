"""Validation of ext-eval: label accuracy on the labelled set, the cost table, and the accuracy gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import routing_eval
from main.app import MisrouteLog
from main.config import Settings
from main.run import make_models

HERE = Path(routing_eval.__file__).resolve().parent
ROOT = HERE.parent


@pytest.fixture
def result(tmp_path):
    models = make_models("stub", Settings(), stub_file=HERE / "stub_replies.json")
    return routing_eval.run_eval(routing_eval.load(HERE / "labelled.jsonl"), models,
                                 MisrouteLog(tmp_path / "m.jsonl"))


def test_accuracy_counts_the_three_planted_mistakes(result):
    assert result["n"] == 24
    assert result["accuracy"] == pytest.approx(21 / 24)
    assert {m["request_id"] for m in result["misroutes"]} == {"r11", "r17", "r22"}


def test_routing_costs_less_than_all_large(result):
    a, r = result["policies"]["all-large"], result["policies"]["routed"]
    assert r["cost_cad"] < a["cost_cad"]
    assert r["large_calls"] < a["large_calls"] == 24
    assert r["router_cost_cad"] > 0                    # the router is counted, not left out


def test_large_model_calls_on_out_of_scope_requests_are_counted_by_the_hand_label(result):
    # r22 is out of scope by hand and the stub router labels it hard: one large-model call that routing should save.
    assert result["out_of_scope_large_calls"] == 1


def test_every_labelled_request_has_a_stub_label():
    stub = json.loads((HERE / "stub_replies.json").read_text())["router"]
    assert {r["text"] for r in routing_eval.load(HERE / "labelled.jsonl")} == set(stub)


def test_report_and_gate(tmp_path):
    out = tmp_path / "eval.md"
    ok = subprocess.run([sys.executable, "ext-eval/routing_eval.py", "--out", str(out), "--min-accuracy", "0.8"],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert "| routed |" in out.read_text() and "Label accuracy: 0.88" in out.read_text()
    fail = subprocess.run([sys.executable, "ext-eval/routing_eval.py", "--out", str(out), "--min-accuracy", "0.95"],
                          cwd=ROOT, capture_output=True, text=True)
    assert fail.returncode == 1 and "FAIL" in fail.stdout
