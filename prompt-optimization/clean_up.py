"""Remove what the recipe created. It creates no AWS resources; a run writes only local files under runs/.

    uv run python clean_up.py

The captured runs in output/ stay; they are part of the recipe.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(root: Path = ROOT) -> None:
    runs = root / "runs"
    if runs.exists():
        shutil.rmtree(runs)
        print(f"removed {runs}")
    else:
        print("nothing to remove")


if __name__ == "__main__":
    main()
