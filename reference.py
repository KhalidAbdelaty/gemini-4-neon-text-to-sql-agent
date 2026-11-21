"""Reference calculation and validator for the LearnFlow activation metric.

Nothing in this file is sent to the model. The agent calls validate() only after
it has frozen a query, and a failed check goes back to Neon as the check's name
and rule, never as the expected counts.

    python reference.py      # print both reference tables and confirm SQL and Python agree
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DB_PATH = Path(__file__).resolve().parent / "data" / "learnflow.db"
FMT = "%Y-%m-%d %H:%M:%S"
COHORT_START, COHORT_END = "2026-10-01 00:00:00", "2026-11-01 00:00:00"
REQUIRED = ("acquisition_channel", "eligible_learners", "activated_learners", "activation_rate")

# Written before the first API call and never changed after it. EXISTS keeps one row per learner.
_QUALIFIER = {
    "enrollment": """SELECT 1 FROM enrollments AS e
                     WHERE e.learner_id = c.learner_id
                       AND e.enrolled_at >= c.signup_at
                       AND e.enrolled_at < datetime(c.signup_at, '+7 days')""",
    "completion": """SELECT 1 FROM lesson_events AS le
                     WHERE le.learner_id = c.learner_id
                       AND le.status = 'completed'
                       AND le.event_at >= c.signup_at
                       AND le.event_at < datetime(c.signup_at, '+7 days')""",
}
REFERENCE_SQL = {metric: f"""
WITH cohort AS (
    SELECT learner_id, acquisition_channel, signup_at
    FROM learners
    WHERE signup_at >= '{COHORT_START}' AND signup_at < '{COHORT_END}'
      AND account_type = 'learner'
)
SELECT acquisition_channel,
       COUNT(*) AS eligible_learners,
       SUM(EXISTS ({qualifier})) AS activated_learners
FROM cohort AS c
GROUP BY acquisition_channel
ORDER BY acquisition_channel""" for metric, qualifier in _QUALIFIER.items()}

CHECKS = {
    "required_columns": "The final query must return acquisition_channel, eligible_learners, "
                        "activated_learners, and activation_rate.",
    "same_channels": "The result must have one row for every acquisition channel in the cohort.",
    "eligible_counts": "Eligible learners must be every October 2026 signup (UTC) that is not a staff or "
                       "test account, counted once per channel.",
    "denominator_unchanged": "The eligible-learner counts must not change when only the activation "
                             "definition changes.",
    "activated_counts": "The activated-learner counts do not match the business rule.",
    "rates_consistent": "activation_rate must equal activated_learners / eligible_learners as a decimal.",
    "not_above_enrollment_rate": "Completion-based activation cannot exceed enrollment-based activation "
                                 "in any channel, because every completion follows an enrollment.",
    "staff_and_test_excluded": "Staff and test accounts must stay out of both counts.",
    "cohort_boundaries": "Only signups at or after 2026-10-01 00:00:00 and before 2026-11-01 00:00:00 count.",
    "late_october_window": "A learner who signs up late on October 31 still gets a full seven days.",
    "seven_day_boundary": "A qualifying event at exactly signup_at + 7 days falls outside the window.",
    "one_count_per_learner": "Repeated qualifying rows must not count a learner more than once.",
    "started_lessons_ignored": "A started lesson that was not completed does not count as a completion.",
}


def reference_sql(db: Path, metric: str) -> dict[str, tuple[int, int]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {ch: (e, a) for ch, e, a in con.execute(REFERENCE_SQL[metric])}
    finally:
        con.close()


def reference_python(db: Path, metric: str) -> dict[str, tuple[int, int]]:
    """The same metric in plain Python over raw rows, sharing no SQL with the query above."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    learners = con.execute("SELECT learner_id, signup_at, acquisition_channel, account_type FROM learners").fetchall()
    if metric == "enrollment":
        events = con.execute("SELECT learner_id, enrolled_at FROM enrollments").fetchall()
    else:
        events = con.execute("SELECT learner_id, event_at FROM lesson_events WHERE status = 'completed'").fetchall()
    con.close()
    by_learner: dict[int, list[datetime]] = {}
    for lid, at in events:
        by_learner.setdefault(lid, []).append(datetime.strptime(at, FMT))
    start, end = datetime.strptime(COHORT_START, FMT), datetime.strptime(COHORT_END, FMT)
    out: dict[str, list[int]] = {}
    for lid, signup_text, channel, account in learners:
        signup = datetime.strptime(signup_text, FMT)
        if account != "learner" or not (start <= signup < end):
            continue
        hit = any(signup <= t < signup + timedelta(days=7) for t in by_learner.get(lid, []))
        counts = out.setdefault(channel, [0, 0])
        counts[0] += 1
        counts[1] += hit
    return {ch: (e, a) for ch, (e, a) in sorted(out.items())}


def parse_result(columns: list[str], rows: list[list]) -> dict[str, tuple] | None:
    """Turn a query result into {channel: (eligible, activated, rate)}, or None if columns are missing."""
    index = {c.lower(): i for i, c in enumerate(columns)}
    if not all(c in index for c in REQUIRED):
        return None
    try:
        return {str(r[index["acquisition_channel"]]): (int(r[index["eligible_learners"]]),
                                                        int(r[index["activated_learners"]]),
                                                        float(r[index["activation_rate"]])) for r in rows}
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- probes
# Each probe copies the database, plants a few rows, and compares how the candidate SQL and the
# reference SQL move. A count can match on the base data by coincidence; a probe isolates one rule.

def _ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def _pick(con, metric: str, activated: bool) -> tuple[int, str, str]:
    """An eligible learner who is (or is not) activated under the metric, with an in-window enrollment."""
    q = REFERENCE_SQL[metric].split("SELECT acquisition_channel,")[0] + f"""
        SELECT c.learner_id, c.signup_at, c.acquisition_channel FROM cohort AS c
        WHERE {'' if activated else 'NOT '}EXISTS ({_QUALIFIER[metric]})
          AND EXISTS ({_QUALIFIER['enrollment']})
        ORDER BY c.learner_id LIMIT 1"""
    row = con.execute(q).fetchone()
    if row is None:  # enrollment metric: a non-activated learner has no in-window enrollment by definition
        row = con.execute(REFERENCE_SQL[metric].split("SELECT acquisition_channel,")[0] + f"""
            SELECT c.learner_id, c.signup_at, c.acquisition_channel FROM cohort AS c
            WHERE NOT EXISTS ({_QUALIFIER[metric]}) ORDER BY c.learner_id LIMIT 1""").fetchone()
    return row


def _qualify(con, lid: int, at: datetime, metric: str, course: str = "C99") -> None:
    """Insert one qualifying event for the metric (with an earlier enrollment if needed)."""
    if metric == "enrollment":
        con.execute("INSERT INTO enrollments (learner_id, course_id, enrolled_at) VALUES (?, ?, ?)",
                    (lid, course, _ts(at)))
    else:
        con.execute("INSERT INTO enrollments (learner_id, course_id, enrolled_at) VALUES (?, ?, ?)",
                    (lid, course, _ts(at - timedelta(minutes=30))))
        con.execute("INSERT INTO lesson_events (learner_id, course_id, lesson_id, status, event_at) "
                    "VALUES (?, ?, ?, 'completed', ?)", (lid, course, f"{course}-L01", _ts(at)))


def _new_learner(con, signup: datetime, channel: str, account: str = "learner") -> int:
    cur = con.execute("INSERT INTO learners (signup_at, acquisition_channel, account_type) VALUES (?, ?, ?)",
                      (_ts(signup), channel, account))
    return cur.lastrowid


def _plant(probe: str, con, metric: str) -> None:
    channel = con.execute("SELECT MIN(acquisition_channel) FROM learners").fetchone()[0]
    if probe == "staff_and_test_excluded":
        for account in ("staff", "test"):
            lid = _new_learner(con, datetime(2026, 10, 15, 10), channel, account)
            _qualify(con, lid, datetime(2026, 10, 15, 12), metric)
    elif probe == "cohort_boundaries":
        for signup in (datetime(2026, 9, 30, 23, 59, 59), datetime(2026, 11, 1)):
            lid = _new_learner(con, signup, channel)
            _qualify(con, lid, signup + timedelta(hours=2), metric)
    elif probe == "late_october_window":
        lid = _new_learner(con, datetime(2026, 10, 31, 23), channel)
        _qualify(con, lid, datetime(2026, 11, 6, 12), metric)
    elif probe == "seven_day_boundary":
        lid, signup, _ = _pick(con, metric, activated=False)
        _qualify(con, lid, datetime.strptime(signup, FMT) + timedelta(days=7), metric)
    elif probe == "one_count_per_learner":
        lid, signup, _ = _pick(con, metric, activated=True)
        for hours in (3, 4, 5):
            _qualify(con, lid, datetime.strptime(signup, FMT) + timedelta(hours=hours), metric)
    elif probe == "started_lessons_ignored":
        lid, signup, _ = _pick(con, metric, activated=False)
        con.execute("INSERT INTO lesson_events (learner_id, course_id, lesson_id, status, event_at) "
                    "SELECT ?, course_id, course_id || '-L09', 'started', datetime(enrolled_at, '+1 hour') "
                    "FROM enrollments WHERE learner_id = ? AND enrolled_at < datetime(?, '+7 days') "
                    "ORDER BY enrolled_at LIMIT 1", (lid, lid, signup))


PROBES = {"enrollment": ["staff_and_test_excluded", "cohort_boundaries", "late_october_window",
                         "seven_day_boundary", "one_count_per_learner"],
          "completion": ["staff_and_test_excluded", "cohort_boundaries", "late_october_window",
                         "seven_day_boundary", "one_count_per_learner", "started_lessons_ignored"]}


def validate(sql: str | None, result: tuple[list, list] | None, metric: str,
             execute: Callable[[Path, str], dict], original: dict | None = None,
             db: Path = DB_PATH) -> dict:
    """Run every check on a frozen query. Returns checks plus the reference table for the harness only."""
    ref = reference_sql(db, metric)
    got = parse_result(*result) if result else None
    checks: list[dict] = []

    def add(name: str, passed: bool) -> None:
        checks.append({"name": name, "passed": bool(passed), "rule": CHECKS[name]})

    add("required_columns", got is not None)
    if got is not None:
        add("same_channels", set(got) == set(ref))
        add("eligible_counts", all(got.get(ch, (None,))[0] == e for ch, (e, _) in ref.items()))
        if original is not None:
            add("denominator_unchanged", all(got.get(ch, (None,))[0] == v[0] for ch, v in original.items()))
        add("activated_counts", all(got.get(ch, (None, None))[1] == a for ch, (_, a) in ref.items()))
        add("rates_consistent", all(e > 0 and abs(r - a / e) <= 0.0005 for e, a, r in got.values()))
        if original is not None:
            add("not_above_enrollment_rate",
                all(ch in original and a <= original[ch][1] for ch, (_, a, _) in got.items()))
        with tempfile.TemporaryDirectory() as tmp:
            for probe in PROBES[metric]:
                copy = Path(tmp) / f"{probe}.db"
                shutil.copyfile(db, copy)
                con = sqlite3.connect(copy)
                _plant(probe, con, metric)
                con.commit()
                con.close()
                out = execute(copy, sql)
                moved = parse_result(out["columns"], out["rows"]) if "rows" in out else None
                expected = reference_sql(copy, metric)
                # Compare how the counts move, so one wrong rule fails only its own probe.
                add(probe, moved is not None and all(
                    tuple(m - b for m, b in zip(moved.get(ch, (0, 0))[:2], got.get(ch, (0, 0))[:2]))
                    == tuple(x - y for x, y in zip(v, ref.get(ch, (0, 0)))) for ch, v in expected.items()))
    return {"checks": checks, "passed": all(c["passed"] for c in checks),
            "reference": {ch: {"eligible": e, "activated": a, "rate": round(a / e, 4)} for ch, (e, a) in ref.items()}}


def feedback(checks: list[dict]) -> str:
    """The only validator text Neon ever sees: failing check names and their rules, no counts."""
    failed = [c for c in checks if not c["passed"]]
    lines = "\n".join(f"- {c['name']}: {c['rule']}" for c in failed)
    return ("An independent check of your revised query failed. Failing checks:\n"
            f"{lines}\nFix the query, run it again with run_sql, and give the corrected results.")


if __name__ == "__main__":
    for metric in ("enrollment", "completion"):
        sql_counts, py_counts = reference_sql(DB_PATH, metric), reference_python(DB_PATH, metric)
        print(f"{metric}: SQL and Python agree = {sql_counts == py_counts}")
        for ch, (e, a) in sql_counts.items():
            print(f"  {ch:<15} {a:>4} / {e:<4} = {a / e:.4f}")
