"""ext-eval-matrix/groups.py against the worked numbers of the README's examples (counted by hand, p and interval
computed with the method the README states)."""

import math

import pytest
from conftest import load_script

groups = load_script("ext-eval-matrix/groups.py")


def cases(group_runs: list[tuple[str, str, int]], runs: int = 3) -> list[dict]:
    """Counted cases from (case, group, runs passed of 3); every case answerable."""
    rows = []
    for case, group, passed in group_runs:
        for k in range(runs):
            rows.append({"case": case, "group": group, "answerable": True, "run": k + 1,
                         "outcome": "correct" if k < passed else "wrong", "refused": False})
    return groups.case_outcomes(rows)


def test_a_case_passes_in_2_of_3_runs():
    assert groups.runs_needed(3) == 2
    assert [c["passed"] for c in cases([("a", "g", 3), ("b", "g", 2), ("c", "g", 1), ("d", "g", 0)])] == \
        [True, True, False, False]


def test_three_outcomes_per_group():
    runs = {  # case: (group, answerable, runs) with r = right value, w = wrong value, x = CANNOT_ANSWER
        "c01": ("one-zone", True, "rrr"), "c02": ("one-zone", True, "rww"), "c03": ("one-zone", True, "xxr"),
        "c04": ("one-zone", True, "rrw"), "c05": ("one-zone", False, "xxw"),
        "c06": ("time-window", True, "rrr"), "c07": ("time-window", True, "www"),
        "c08": ("time-window", False, "wwx"), "c09": ("time-window", True, "rrw"), "c10": ("time-window", True, "rrr"),
    }
    rows = []
    for case, (group, answerable, rs) in runs.items():
        for k, r in enumerate(rs):
            if answerable:
                outcome = "correct" if r == "r" else "wrong"
            else:
                outcome = "correct refusal" if r == "x" else "wrong"
            rows.append({"case": case, "group": group, "answerable": answerable, "run": k + 1,
                         "outcome": outcome, "refused": r == "x"})
    table = {r["group"]: (r["correct"], r["correct refusal"], r["wrong"], r["refused"], r["passed"], r["cases"])
             for r in groups.group_rows(groups.case_outcomes(rows))}
    assert table == {"one-zone": (2, 1, 2, 1, 3, 5), "time-window": (3, 0, 2, 0, 3, 5), "all": (5, 1, 4, 1, 6, 10)}


@pytest.mark.parametrize("b, a, p", [(6, 0, 0.03125), (5, 0, 0.0625), (1, 0, 1.0), (0, 0, 1.0), (3, 0, 0.25),
                                     (4, 2, 0.6875)])
def test_exact_mcnemar_p_is_the_binomial_test(b, a, p):
    assert groups.mcnemar_p(b, a) == pytest.approx(p)
    n = a + b   # the same p from the binomial distribution itself
    if n:
        tail = sum(math.comb(n, k) for k in range(min(a, b) + 1)) / 2 ** n
        assert groups.mcnemar_p(b, a) == pytest.approx(min(1.0, 2 * tail))


SEED_AFTER = [("c06", "time-window", 3, 3), ("c07", "time-window", 1, 2), ("c08", "time-window", 3, 3),
              ("c09", "time-window", 0, 0), ("c10", "time-window", 3, 3), ("c11", "zone-compare", 0, 3),
              ("c12", "zone-compare", 1, 2), ("c13", "zone-compare", 0, 3), ("c14", "zone-compare", 1, 3),
              ("c15", "zone-compare", 0, 2)]


def test_the_comparison_of_the_readme_example():
    a = cases([(c, g, x) for c, g, x, _ in SEED_AFTER])
    b = cases([(c, g, y) for c, g, _, y in SEED_AFTER])
    result = groups.compare(a, b, seed=0)
    rows = {r["group"]: r for r in result["rows"]}
    assert (rows["time-window"]["a_passed"], rows["time-window"]["b_passed"], rows["time-window"]["b_only"],
            rows["time-window"]["a_only"]) == (3, 4, 1, 0)
    assert f"{rows['time-window']['difference']:+.2f}" == "+0.20" and f"{rows['time-window']['p']:.4f}" == "1.0000"
    assert f"{rows['zone-compare']['p']:.4f}" == "0.0625" and rows["zone-compare"]["too_few"]
    assert f"{rows['all']['difference']:+.2f}" == "+0.60" and f"{rows['all']['p']:.4f}" == "0.0312"
    assert not rows["all"]["too_few"]
    assert f"{result['runs_difference']:+.2f}" == "+0.40"
    low, high = result["interval"]
    assert (f"{low:+.2f}", f"{high:+.2f}") == ("+0.27", "+0.53")
    assert "interval" not in rows["time-window"]          # the per-group interval is off unless asked for


def test_the_same_runs_and_seed_give_the_same_table():
    a = cases([(c, g, x) for c, g, x, _ in SEED_AFTER])
    b = cases([(c, g, y) for c, g, _, y in SEED_AFTER])
    one = groups.comparison_table("A", "B", groups.compare(a, b, seed=0))
    two = groups.comparison_table("A", "B", groups.compare(a, b, seed=0))
    assert one == two
    assert groups.comparison_table("A", "B", groups.compare(a, b, seed=1)) != one   # the seed is what fixes it


def test_per_group_interval_only_behind_the_flag():
    a = cases([(c, g, x) for c, g, x, _ in SEED_AFTER])
    b = cases([(c, g, y) for c, g, _, y in SEED_AFTER])
    result = groups.compare(a, b, seed=0, group_intervals=True)
    assert all("interval" in r for r in result["rows"] if r["group"] != "all")
    assert "interval (descriptive)" in groups.comparison_table("A", "B", result)


def test_cells_on_different_cases_are_not_compared():
    with pytest.raises(ValueError, match="same cases"):
        groups.compare(cases([("a", "g", 3)]), cases([("b", "g", 3)]))


def test_cost_and_time_of_six_answers():
    rows = [{"seconds": s, "cad": 0.0001} for s in (1.2, 1.4, 1.5, 2.0, 2.2, 9.0)]
    c = groups.cost_time(rows)
    assert c["cad_per_answer"] == pytest.approx(0.0001)
    assert c["median_s"] == pytest.approx(1.75) and c["p95_s"] == 9.0
