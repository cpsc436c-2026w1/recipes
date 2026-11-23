"""Remove what the recipe created. It creates no AWS resources; a run writes only local files under runs/.

    uv run python clean_up.py    # lists what it would delete; deletes nothing
    uv run python clean_up.py --yes

The captured runs in output/ stay; they are part of the recipe.
"""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None, root: Path = ROOT) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    a = p.parse_args(argv)
    dry = a.dry_run or not a.yes
    runs = root / "runs"
    if runs.exists():
        if dry:
            print(f"would remove {runs}")
        else:
            shutil.rmtree(runs)
            print(f"removed {runs}")
    else:
        print("nothing to remove")
    if dry:
        print("Nothing deleted. Run again with --yes to delete these.")


if __name__ == "__main__":
    main()
