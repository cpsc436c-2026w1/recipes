"""Remove what a run created. This recipe creates nothing in AWS: only local log files.

    uv run python clean_up.py    # what would go
    uv run python clean_up.py --yes              # delete the .jsonl logs in output/ (misroutes, routes, eval-misroutes)

The captured runs (output/*.txt, output/*.md) stay; they are part of the recipe.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None, root: Path = ROOT) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    a = p.parse_args(argv)
    a.dry_run = a.dry_run or not a.yes
    for path in sorted((root / "output").glob("*.jsonl")):
        print(("would delete " if a.dry_run else "deleted ") + str(path.relative_to(root)))
        if not a.dry_run:
            path.unlink()
    if a.dry_run:
        print("Nothing deleted. Run again with --yes to delete these.")


if __name__ == "__main__":
    main()
