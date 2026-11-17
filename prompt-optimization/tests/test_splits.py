"""Split discipline: the optimizer never sees a test question, and test questions are not copies of dev ones."""

import ast
import re

import dspy
import yaml

from conftest import ROOT, load_script
from main import data, run, stub


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def test_splits_are_disjoint():
    dev = yaml.safe_load((ROOT / "data/questions.yml").read_text())
    test = yaml.safe_load((ROOT / "data/test.yml").read_text())
    assert {q["split"] for q in dev} == {"train", "val"}
    assert not {q["id"] for q in dev} & {q["id"] for q in test}
    assert not {norm(q["question"]) for q in dev} & {norm(q["question"]) for q in test}
    # A test question with the same reference SQL as a train question would be answered by copying.
    assert not {norm(q["sql"]) for q in dev} & {norm(q["sql"]) for q in test}


def test_optimize_module_does_not_load_test():
    tree = ast.parse((ROOT / "main/optimize.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "load_test" not in names
    assert "test.yml" not in (ROOT / "main/optimize.py").read_text()


def _record_calls(monkeypatch, module):
    """Log every question the stub task model answers, and the moment the test split is loaded."""
    log = []
    real_reply, real_load = stub.task_reply, module.load_test

    def reply(model, system, user):
        log.append(("call", user))
        return real_reply(model, system, user)

    def load():
        log.append(("load_test", None))
        return real_load()

    monkeypatch.setattr(stub, "task_reply", reply)
    monkeypatch.setattr(module, "load_test", load)
    return log


def _check_log(log):
    test_questions = [q["question"] for q in data.load_test()]
    marks = [i for i, (kind, _) in enumerate(log) if kind == "load_test"]
    assert len(marks) == 1, "the test split is loaded exactly once, for the report"
    before = [text for kind, text in log[: marks[0]] if kind == "call"]
    after = [text for kind, text in log[marks[0]:] if kind == "call"]
    assert before, "the optimizer made task-model calls"
    assert not [q for q in test_questions for text in before if q in text], "a test question reached the optimizer"
    assert all(any(q in text for text in after) for q in test_questions), "every test question is in the report"


def test_main_optimizer_never_sees_test(monkeypatch, tmp_path):
    seen = {}
    real_compile = dspy.GEPA.compile

    def compile_spy(self, student, *, trainset, valset=None, **kw):
        seen["ids"] = {ex.id for ex in trainset} | {ex.id for ex in valset or []}
        return real_compile(self, student, trainset=trainset, valset=valset, **kw)

    monkeypatch.setattr(dspy.GEPA, "compile", compile_spy)
    log = _record_calls(monkeypatch, run)
    run.main(["--runs", "1", "--out", str(tmp_path)])
    assert seen["ids"] and not seen["ids"] & {q["id"] for q in data.load_test()}
    _check_log(log)


def test_direct_optimizer_never_sees_test(monkeypatch, tmp_path):
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    log = _record_calls(monkeypatch, direct)
    direct.main(["--runs", "1", "--out", str(tmp_path)])
    _check_log(log)
