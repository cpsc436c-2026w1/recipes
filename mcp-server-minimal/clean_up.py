"""Remove what a run created. This recipe creates nothing in AWS: only local files.

    uv run python clean_up.py --dry-run    # what would go
    uv run python clean_up.py              # delete data/*.parquet and output/refunds.duckdb

The captured runs in output/*.txt stay; they are part of the recipe.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def targets(root: Path = ROOT) -> list[Path]:
    return sorted([*(root / "data").glob("*.parquet"), *(root / "output").glob("*.duckdb*")])


def main(argv: list[str] | None = None, root: Path = ROOT) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    for path in targets(root):
        print(("would delete " if a.dry_run else "deleted ") + str(path.relative_to(root)))
        if not a.dry_run:
            path.unlink()


if __name__ == "__main__":
    main()
