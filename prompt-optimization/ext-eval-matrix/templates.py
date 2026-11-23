"""The cell without a model, `no-model/templates`: fixed SQL templates filled from the question.

A regular expression on the question picks the template and fills its zone id, its times and its day; a question
no template matches gets CANNOT_ANSWER. No model call, 0 CAD. This is the version of the step that could ship
without a model, scored by the same metric on the same cases (README, Decisions: "A cell without a model").

Write your own templates for the questions your users ask most; the matrix then shows, group by group, what the
model cells add over them.
"""

from __future__ import annotations

import re

DAY = "2024-03-14"          # the day of the table; a question that names another day fills that day instead

ZONE = r"zone (\d+)"
WINDOW = r"between (\d\d:\d\d) and (\d\d:\d\d)"
ON_DAY = r"on (\d{4}-\d\d-\d\d)"


def _window(m_window, day: str) -> str:
    start, end = m_window.groups()
    return (f"requested_time >= TIMESTAMP '{day} {start}:00' AND requested_time < TIMESTAMP '{day} {end}:00'")


def answer(question: str) -> str:
    """The query for `question`, or CANNOT_ANSWER."""
    q = question.strip()
    zone, window, day = re.search(ZONE, q), re.search(WINDOW, q), re.search(ON_DAY, q)
    the_day = day.group(1) if day else DAY
    if re.match(r"how many trips were requested", q, flags=re.I) and len(re.findall(ZONE, q)) <= 1:
        where = []
        if zone:
            where.append(f"pickup_zone_id = {int(zone.group(1))}")
        if window:
            where.append(_window(window, the_day))
        if where or re.fullmatch(r"how many trips were requested\?", q, flags=re.I):
            return "SELECT COUNT(*) FROM trips" + (" WHERE " + " AND ".join(where) if where else "")
    if re.search(r"average fare", q, flags=re.I) and zone and len(re.findall(ZONE, q)) == 1:
        return f"SELECT AVG(fare) FROM trips WHERE pickup_zone_id = {int(zone.group(1))}"
    if re.search(r"total of all fares", q, flags=re.I):
        return "SELECT SUM(fare) FROM trips"
    return "CANNOT_ANSWER"
