"""The main scenario end to end on the stub: GEPA runs, and the report has the before and after rows."""

import json
import re

from main import run
from main.program import SEED_INSTRUCTIONS


def test_loop_produces_before_after_table(tmp_path):
    out = run.main(["--runs", "2", "--out", str(tmp_path)])
    report = (out / "report.md").read_text()
    rows = dict(re.findall(r"^\| (before|after) \| (\d+)% \|", report, flags=re.M))
    assert set(rows) == {"before", "after"}
    # The stub's habits are stopped by rules the stub reflection writes, so the optimized prompt passes more.
    assert int(rows["after"]) > int(rows["before"])
    after = (out / "instructions_after.txt").read_text()
    assert after.strip() != SEED_INSTRUCTIONS.strip()
    assert after.startswith(SEED_INSTRUCTIONS.splitlines()[0])   # GEPA extended the seed, it kept its first line
    assert (out / "program.json").exists()
    cands = json.loads((out / "candidates.json").read_text())
    assert cands[0]["instructions"] == SEED_INSTRUCTIONS        # candidate 0 is the seed
    assert "| total |" in report and "CAD" in report


def test_wrong_value_is_not_fixed_by_the_prompt(tmp_path):
    """q06 is wrong in the stub whatever the prompt says: the report keeps a failure an honest run would show."""
    out = run.main(["--runs", "1", "--out", str(tmp_path)])
    answers = json.loads((out / "answers.json").read_text())
    assert [r["kind"] for r in answers["after"] if r["id"] == "q06"] == ["wrong value"]
