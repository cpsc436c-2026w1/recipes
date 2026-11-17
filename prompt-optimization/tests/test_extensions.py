"""Both extensions run end to end on the stub."""

import re

from conftest import load_script
from main import run


def test_gepa_direct_improves_the_prompt_string(tmp_path):
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    out = direct.main(["--runs", "1", "--out", str(tmp_path)])
    report = (out / "report.md").read_text()
    rows = dict(re.findall(r"^\| (before|after) \| (\d+)% \|", report, flags=re.M))
    assert int(rows["after"]) > int(rows["before"])
    assert (out / "instructions_after.txt").read_text().startswith(direct.SEED_INSTRUCTIONS)


def test_extract_sql_takes_the_code_block():
    direct = load_script("ext-gepa-direct/optimize_direct.py")
    assert direct.extract_sql("Here:\n```sql\nSELECT 1\n```\nDone.") == "SELECT 1"
    assert direct.extract_sql("SELECT 1") == "SELECT 1"


def test_eval_matrix_has_one_row_per_cell(tmp_path):
    first = run.main(["--runs", "1", "--out", str(tmp_path / "opt")])
    matrix = load_script("ext-eval-matrix/matrix.py")
    out = matrix.main(["--runs", "2", "--prompt", f"after={first / 'instructions_after.txt'}",
                       "--out", str(tmp_path / "matrix")])
    report = (out / "report.md").read_text()
    cells = re.findall(r"^\| (\S+) \(stub\) \| (seed|after) \| (\d+)% \|.*\| ([\d.]+) \| ([\d.]+) \|$", report,
                       flags=re.M)
    assert len(cells) == 4                                    # 2 models x 2 prompts
    by = {(m, p): (int(pct), float(cad)) for m, p, pct, cad, _ in cells}
    assert by[("qwen3.6-35b-a3b", "after")][0] > by[("qwen3.6-35b-a3b", "seed")][0]
    # The larger model costs more per answer on the same prompt (the price table, not a measurement).
    assert by[("qwen3.8-27b", "seed")][1] > by[("qwen3.6-35b-a3b", "seed")][1]
