"""Compare two runs of main/probe.py: which rows changed between them.

    uv run python main/probe.py --json monday.json      # one run
    uv run python main/probe.py --json today.json       # the same endpoint later, or another endpoint
    uv run python main/compare.py monday.json today.json

Prints one line per probe and model whose `works` differs between the two files (a row that is in only one of
them counts as `not run` in the other), and exits with code 1 when there is such a line. It reads the two files
and nothing else: no request is sent. README.md, "Decisions": compare two runs.
"""

import argparse
import json
import sys


def works_by_row(path):
    """(probe, model) -> works, from a file that main/probe.py --json wrote."""
    with open(path) as f:
        return {(row["probe"], row["model"]): row["works"] for row in json.load(f)}


def differences(first, second):
    """(probe, model, works in the first, works in the second) for every row that differs, in the first's order."""
    keys = list(first) + [key for key in second if key not in first]
    return [(probe, model, first.get((probe, model), "not run"), second.get((probe, model), "not run"))
            for probe, model in keys
            if first.get((probe, model), "not run") != second.get((probe, model), "not run")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first", help="a file written by main/probe.py --json")
    parser.add_argument("second", help="a later run, or a run against another endpoint")
    args = parser.parse_args(argv)
    changed = differences(works_by_row(args.first), works_by_row(args.second))
    if not changed:
        print(f"no row differs between {args.first} and {args.second}")
        return 0
    print(f"| probe | model | {args.first} | {args.second} |")
    print("|---|---|---|---|")
    for probe, model, before, after in changed:
        print(f"| {probe} | {model} | {before} | {after} |")
    print(f"{len(changed)} row(s) differ")
    return 1


if __name__ == "__main__":
    sys.exit(main())
