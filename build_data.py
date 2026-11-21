"""Build the synthetic LearnFlow SQLite database with a fixed seed.

    python build_data.py

Writes data/learnflow.db. Every number in it is synthetic. Running the script
again with the same seed rebuilds the same rows.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20261101
DB_PATH = Path(__file__).resolve().parent / "data" / "learnflow.db"
FMT = "%Y-%m-%d %H:%M:%S"  # one UTC text format everywhere, so text comparisons stay valid
WEEK = timedelta(days=7)
COURSES = [f"C{n:02d}" for n in range(1, 13)]

# channel: (share of signups, chance of enrolling within 7 days, chance a 7-day enrollee completes a lesson in time)
CHANNELS = {
    "organic_search": (0.30, 0.62, 0.55),
    "paid_social": (0.25, 0.70, 0.30),
    "referral": (0.15, 0.55, 0.72),
    "email": (0.15, 0.48, 0.50),
    "partner": (0.15, 0.66, 0.40),
}

SCHEMA = """
CREATE TABLE learners (
    learner_id INTEGER PRIMARY KEY,
    signup_at TEXT NOT NULL,
    acquisition_channel TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('learner', 'staff', 'test'))
);
CREATE TABLE enrollments (
    enrollment_id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners (learner_id),
    course_id TEXT NOT NULL,
    enrolled_at TEXT NOT NULL
);
CREATE TABLE lesson_events (
    event_id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners (learner_id),
    course_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
    event_at TEXT NOT NULL
);
"""


def between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """A whole-second time in [start, end)."""
    return start + timedelta(seconds=rng.randrange(int((end - start).total_seconds())))


def build() -> dict:
    rng = random.Random(SEED)
    learners, enrollments, events = [], [], []

    # 1,040 October signups plus 160 just outside the month, so the cohort filter matters.
    windows = ([(datetime(2026, 10, 1), datetime(2026, 11, 1))] * 1040
               + [(datetime(2026, 9, 24), datetime(2026, 10, 1))] * 80
               + [(datetime(2026, 11, 1), datetime(2026, 11, 6))] * 80)
    signups = sorted(between(rng, a, b) for a, b in windows)
    # Pin the exact month edges: first and last second of October, and one second on either side.
    signups[0:2] = [datetime(2026, 9, 30, 23, 59, 59), datetime(2026, 10, 1, 0, 0, 0)]
    signups.sort()
    signups[-1] = datetime(2026, 11, 1, 0, 0, 0)
    signups[-2] = datetime(2026, 10, 31, 23, 59, 59)
    signups.sort()

    names, weights = list(CHANNELS), [v[0] for v in CHANNELS.values()]

    def add_event(lid, course, lesson, status, at):
        events.append((lid, course, f"{course}-L{lesson:02d}", status, at))

    for lid, signup in enumerate(signups, start=1):
        channel = rng.choices(names, weights)[0]
        roll = rng.random()
        account = "staff" if roll < 0.03 else "test" if roll < 0.06 else "learner"
        learners.append((lid, signup.strftime(FMT), channel, account))
        _, p_enroll, p_complete = CHANNELS[channel]
        if account != "learner":  # staff and test accounts click through everything
            p_enroll, p_complete = 0.9, 0.8

        if rng.random() < p_enroll:
            course = rng.choice(COURSES)
            enrolled = between(rng, signup + timedelta(minutes=5), signup + WEEK - timedelta(hours=1))
            enrollments.append((lid, course, enrolled))
            if rng.random() < 0.10:  # the same course enrolled twice
                enrollments.append((lid, course, between(rng, enrolled, enrolled + timedelta(days=3))))
            if rng.random() < 0.25:  # a second course, sometimes after the window
                enrollments.append((lid, rng.choice(COURSES), between(rng, enrolled, signup + timedelta(days=15))))

            if rng.random() < p_complete:
                for lesson in range(1, rng.choice([1, 1, 2, 3, 4]) + 1):  # one or more completed lessons
                    done = between(rng, enrolled + timedelta(minutes=10), signup + WEEK)
                    add_event(lid, course, lesson, "started", between(rng, enrolled, done))
                    add_event(lid, course, lesson, "completed", done)
                    if rng.random() < 0.15:  # the same lesson completed again
                        add_event(lid, course, lesson, "completed", between(rng, done, signup + WEEK))
            else:
                if rng.random() < 0.5:  # started inside the window, never finished in time
                    add_event(lid, course, 1, "started", between(rng, enrolled, signup + WEEK))
                if rng.random() < 0.4:  # finished, but after the window closed
                    add_event(lid, course, 1, "completed",
                              between(rng, signup + WEEK, signup + timedelta(days=20)))
                elif rng.random() < 0.06:  # finished on the exact seven-day boundary, which does not count
                    add_event(lid, course, 1, "completed", signup + WEEK)
        elif rng.random() < 0.45:  # enrolled late, sometimes exactly on the boundary
            course = rng.choice(COURSES)
            late = signup + WEEK if rng.random() < 0.08 else between(rng, signup + WEEK, signup + timedelta(days=18))
            enrollments.append((lid, course, late))
            if rng.random() < 0.5:
                add_event(lid, course, 1, "completed", between(rng, late, late + timedelta(days=4)))

    enrollments.sort(key=lambda r: r[2])
    events.sort(key=lambda r: r[4])
    check_rules(learners, enrollments, events)

    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO learners VALUES (?, ?, ?, ?)", learners)
    con.executemany("INSERT INTO enrollments (learner_id, course_id, enrolled_at) VALUES (?, ?, ?)",
                    [(l, c, t.strftime(FMT)) for l, c, t in enrollments])
    con.executemany("INSERT INTO lesson_events (learner_id, course_id, lesson_id, status, event_at) "
                    "VALUES (?, ?, ?, ?, ?)", [(l, c, s_, st, t.strftime(FMT)) for l, c, s_, st, t in events])
    con.commit()
    summary = {
        "learners": len(learners),
        "october_signups": sum(1 for r in learners if r[1].startswith("2026-10")),
        "staff_or_test": sum(1 for r in learners if r[3] != "learner"),
        "enrollments": len(enrollments),
        "lesson_events": len(events),
        "last_event_at": con.execute("SELECT MAX(event_at) FROM lesson_events").fetchone()[0],
        "channels": sorted({r[2] for r in learners}),
    }
    con.close()
    return summary


def check_rules(learners, enrollments, events) -> None:
    """Fail the build if the data breaks the rules the experiment depends on."""
    signup = {r[0]: datetime.strptime(r[1], FMT) for r in learners}
    first_enroll: dict[tuple[int, str], datetime] = {}
    for lid, course, at in enrollments:
        assert at >= signup[lid], "an enrollment precedes its learner's signup"
        first_enroll[(lid, course)] = min(at, first_enroll.get((lid, course), at))
    for lid, course, _, _, at in events:
        assert (lid, course) in first_enroll and at >= first_enroll[(lid, course)], \
            "a lesson event has no earlier enrollment in the same course"
    # Every October signup gets a full seven days of data.
    last_october = max(t for t in signup.values() if t.month == 10)
    assert max(e[4] for e in events) >= last_october + WEEK, "data ends before the last follow-up window"


if __name__ == "__main__":
    for key, value in build().items():
        print(f"{key:>16}: {value}")
    print(f"Wrote {DB_PATH}")
