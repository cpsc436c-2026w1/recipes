"""The cost of a run, of 30 days and of the horizon, from a run's units and prices.toml.

    uv run python scripts/cost.py --run-id <id>            # evidence/<run-id>/cost.md, and printed

The stated rates are settings (defaults are the recipe's assumptions; change them to your own):
    RUNS_PER_DAY=1     runs a day while the system is left on, in a busy month
    DEMO_HOURS=10      hours a month the system is up for tests and demonstrations (one run each)
    BUSY_MONTHS=4      busy months of 30 days in the horizon; the other days are idle (no runs)
    HORIZON_DAYS=365   the horizon
    COST_LIMIT_USD=30  the cost limit the team accepts (the group lease budget)

Always on: every hourly part kept for every hour (720 in 30 days), the run repeated at the stated rate.
Demonstrations only: hourly parts only for the demonstration hours, one run per hour, everything deleted between.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = Path(__import__("os").environ.get("EVIDENCE") or ROOT / "evidence")
PRICES = ROOT / "prices.toml"
ATHENA = "Athena Data scanned, MB"
ATHENA_MIN_MB = 10


def prices(path: Path = PRICES) -> dict:
    return tomllib.loads(path.read_text())


def run_rows(parts: list[dict], p: dict) -> list[dict]:
    """One row per billed part: its units, rate and cost in USD and CAD, or "unpriced"."""
    rows = []
    for x in parts:
        if "not measured" in x:
            rows.append({"part": x["not measured"], "units": None, "usd": None, "note": "not measured"})
            continue
        name, units = x["part"], x["units"]
        label = name
        if name == ATHENA:
            units = max(units, ATHENA_MIN_MB * x.get("queries", 1))
            label = f"Athena, billed {units:g} MB"
        rate = p["rates"].get(name)
        if rate is None:
            rows.append({"part": label, "units": units, "usd": None, "note": "unpriced"})
            continue
        usd = units * rate[0] / rate[1]
        rows.append({"part": label, "units": units, "usd": usd, "cad": usd * p["cad_per_usd"],
                     "rate": f"{rate[0]} USD per {rate[1]:,}" if rate[1] != 1 else f"{rate[0]} USD each",
                     "note": f"{p['sheet']}", "hourly": x.get("hourly", False), "count": x.get("count", 0),
                     "unit_rate": rate[0] / rate[1], "how": x.get("how", "")})
    return rows


def total(rows: list[dict]) -> tuple[float, bool]:
    """The sum of the priced rows, and whether it is complete (no unpriced or unmeasured row)."""
    return sum(r["usd"] or 0 for r in rows), all(r["usd"] is not None for r in rows)


def thirty_days(hourly: list[tuple[str, int, float]], run_cost: float, runs_per_day: float = 1,
                demo_hours: float = 10) -> dict:
    """Always on and demonstrations only, over 30 days. hourly: (part, how many, USD per hour each)."""
    on_hourly = sum(n * rate * 720 for _, n, rate in hourly)
    demo_hourly = sum(n * rate * demo_hours for _, n, rate in hourly)
    return {"always_on": on_hourly + 30 * runs_per_day * run_cost, "always_on_hourly": on_hourly,
            "demo": demo_hourly + demo_hours * run_cost, "demo_hourly": demo_hourly}


def horizon(hourly: list[tuple[str, int, float]], run_cost: float, runs_per_day: float = 1, demo_hours: float = 10,
            busy_months: int = 4, days: int = 365, limit: float = 30.0) -> dict:
    """The horizon from busy and idle months (busy months first), and the month in which each column passes the
    limit (None if it never does)."""
    busy = thirty_days(hourly, run_cost, runs_per_day, demo_hours)
    idle_on = sum(n * rate * 720 for _, n, rate in hourly)
    idle_days = days - 30 * busy_months
    out = {}
    for col, busy_month, idle_month in (("always_on", busy["always_on"], idle_on), ("demo", busy["demo"], 0.0)):
        months = [busy_month] * busy_months + [idle_month] * (idle_days // 30)
        if idle_days % 30:
            months.append(idle_month * (idle_days % 30) / 30)
        running, passes = 0.0, None
        for i, m in enumerate(months, 1):
            running += m
            if passes is None and running > limit:
                passes = i
        out[col] = {"busy_month": busy_month, "idle_month": idle_month, "total": running, "passes_in": passes,
                    "months": len(months)}
    return out


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def report(units: dict, p: dict, *, runs_per_day=1, demo_hours=10, busy_months=4, days=365, limit=30.0) -> str:
    rows = run_rows(units["parts"], p)
    usd, complete = total(rows)
    cad = p["cad_per_usd"]
    hourly = [(r["part"], r["count"] or 1, r["unit_rate"]) for r in rows if r.get("hourly")]
    run_cost = sum(r["usd"] for r in rows if r["usd"] is not None and not r.get("hourly"))
    t = thirty_days(hourly, run_cost, runs_per_day, demo_hours)
    h = horizon(hourly, run_cost, runs_per_day, demo_hours, busy_months, days, limit)
    word = "Total" if complete else "Subtotal (a row is unpriced or not measured)"
    lines = [f"# Cost of run {units.get('run_id', '')}", "",
             f"Rates: {p['sheet']}; CAD at {cad}. Units: evidence/{units.get('run_id', '<run>')}/units.json.", "",
             "| Part | Units | Rate | USD | CAD | Source |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r["usd"] is None:
            lines.append(f"| {r['part']} | {'' if r['units'] is None else r['units']} | — | {r['note']} | "
                         f"{r['note']} | — |")
        else:
            lines.append(f"| {r['part']} | {r['units']:g} | {r['rate']} | {r['usd']:.6f} | {r['cad']:.6f} | "
                         f"{r['note']} ({r['how']}) |")
    lines += [f"| **{word}** | | | **{usd:.6f}** | **{usd * cad:.6f}** | |", "",
              f"## 30 days ({runs_per_day:g} run a day left on; {demo_hours:g} demonstration hours)", "",
              "| Column | USD | CAD | of which hourly parts | % of the limit |", "|---|---|---|---|---|",
              f"| Always on | {t['always_on']:.4f} | {t['always_on'] * cad:.4f} | {t['always_on_hourly']:.4f} | "
              f"{100 * t['always_on'] / limit:.1f}% |",
              f"| Demonstrations only | {t['demo']:.4f} | {t['demo'] * cad:.4f} | {t['demo_hourly']:.4f} | "
              f"{100 * t['demo'] / limit:.1f}% |", "",
              f"## Horizon: {days} days, {busy_months} busy months of 30 days, limit {limit:g} USD", "",
              "| Column | Busy month | Idle month | Horizon USD | Horizon CAD | Passes the limit |",
              "|---|---|---|---|---|---|"]
    for col, name in (("always_on", "Always on"), ("demo", "Demonstrations only")):
        c = h[col]
        when = f"in the {ordinal(c['passes_in'])} month" if c["passes_in"] else "never"
        lines.append(f"| {name} | {c['busy_month']:.4f} | {c['idle_month']:.4f} | {c['total']:.2f} | "
                     f"{c['total'] * cad:.2f} | {when} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    ev = EVIDENCE / args.run_id
    units = json.loads((ev / "units.json").read_text())
    text = report(units, prices(), runs_per_day=float(os.environ.get("RUNS_PER_DAY", 1)),
                  demo_hours=float(os.environ.get("DEMO_HOURS", 10)),
                  busy_months=int(os.environ.get("BUSY_MONTHS", 4)), days=int(os.environ.get("HORIZON_DAYS", 365)),
                  limit=float(os.environ.get("COST_LIMIT_USD", 30)))
    (ev / "cost.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
