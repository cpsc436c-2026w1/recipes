"""The clean-up contract (AGENTS.md, "Keep the course constraints"): a bare run lists what it would delete, deletes
nothing and ends with "Nothing deleted. Run again with --yes to delete these."; with --yes it deletes. Run on local
files in a temporary folder."""
import importlib.util
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]
MSG = "Nothing deleted. Run again with --yes to delete these."


def load():
    spec = importlib.util.spec_from_file_location("contract_clean_up", RECIPE / "clean_up.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def files(root: Path) -> list[Path]:
    (root / "data").mkdir()
    (root / "output").mkdir()
    made = [root / "data" / "trips.parquet", root / "output" / "trips.duckdb"]
    for f in made:
        f.write_text("x")
    return made


def test_a_bare_run_deletes_nothing_and_says_so(tmp_path, capsys):
    made = files(tmp_path)
    load().main([], root=tmp_path)
    out = capsys.readouterr().out
    assert all(f.exists() for f in made) and "would delete" in out
    assert out.rstrip().splitlines()[-1] == MSG


def test_yes_deletes(tmp_path, capsys):
    made = files(tmp_path)
    load().main(["--yes"], root=tmp_path)
    assert not any(f.exists() for f in made) and MSG not in capsys.readouterr().out
