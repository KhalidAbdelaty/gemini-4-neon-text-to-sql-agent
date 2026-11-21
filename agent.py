"""LearnFlow SQL analyst: Gemini 4 Neon writes the SQL, Python runs it read-only.

    python agent.py

One run makes the original enrollment-based analysis, sends the lesson-completion
correction with previous_interaction_id, freezes the revised query, and checks it
with reference.py. At most one correction turn follows a failed check. The run is
saved to runs/run_<UTC time>.json and billed to GEMINI_API_KEY from .env.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from dotenv import dotenv_values
from google import genai

import reference

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "learnflow.db"
RUNS = HERE / "runs"

MODEL = "gemini-4-neon"
THINKING_LEVEL = "high"  # the documented default, sent explicitly on every request

# Application limits, checked before every request. The API has no task budget of its own.
MAX_REQUESTS = 20
MAX_TOOL_CALLS = 24
MAX_TOOL_CALLS_PER_PHASE = 10
MAX_SECONDS = 1200
MAX_SPEND_USD = 2.00
MAX_CORRECTIONS = 1
REQUEST_TIMEOUT_S = 900  # Google suggests at least 15 minutes for hard prompts

# Gemini Developer API, paid Standard tier, USD per 1M tokens (checked November 21, 2026).
PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT = 3.00, 0.15, 15.00

# Read-only SQL tool limits.
MAX_SQL_CHARS, MAX_ROWS, MAX_RESULT_CHARS, MAX_QUERY_SECONDS = 8_000, 200, 20_000, 5.0
ALLOWED_TABLES = {"learners", "enrollments", "lesson_events"}

SYSTEM_INSTRUCTION = """You are a SQL analyst for LearnFlow, a fictional online learning platform.
You answer questions by writing SQLite queries and running them with the run_sql tool. The application
runs each query on a read-only copy of the database and returns the rows. Every number in your answer
must come from a run_sql result; do not estimate or invent numbers.

Database schema (SQLite; every timestamp is UTC text in the format 'YYYY-MM-DD HH:MM:SS'):
- learners(learner_id INTEGER PRIMARY KEY, signup_at TEXT, acquisition_channel TEXT, account_type TEXT)
  account_type is 'learner', 'staff', or 'test'.
- enrollments(enrollment_id INTEGER PRIMARY KEY, learner_id INTEGER REFERENCES learners, course_id TEXT,
  enrolled_at TEXT). A learner can enroll in the same course, or in several courses, more than once.
- lesson_events(event_id INTEGER PRIMARY KEY, learner_id INTEGER REFERENCES learners, course_id TEXT,
  lesson_id TEXT, status TEXT, event_at TEXT). status is 'started' or 'completed'. The same lesson can
  appear more than once for a learner.

run_sql accepts one SELECT statement (WITH is allowed) and returns at most 200 rows, so write the query
that returns the final table. When you finish, reply with the final results table, the final SQL in a
```sql block, and a short interpretation."""

ORIGINAL_TASK = """LearnFlow's product team wants to compare learner activation across acquisition channels
for learners who signed up in October 2026.

Business rules:
- Cohort: learners whose signup_at is from 2026-10-01 00:00:00 through 2026-10-31 23:59:59 UTC.
- Exclude staff and test accounts.
- A learner is activated if they enrolled in at least one course within seven days of their own signup
  time: at or after signup_at and before signup_at plus 7 days.
- Group by acquisition channel and count each eligible learner once, however many enrollments they have.
- Activation rate = activated eligible learners / eligible learners in that channel.

Return one row per acquisition_channel with the columns acquisition_channel, eligible_learners,
activated_learners, and activation_rate (a decimal between 0 and 1). Then tell me what the results show."""

REVISION_TASK = """The product team has changed the definition of activation. Enrolling in a course is no
longer enough: a learner now counts as activated only after completing at least one lesson within the same
seven-day window. Keep everything else in the analysis the same, rerun it, and tell me what changed."""

RUN_SQL = {
    "type": "function",
    "name": "run_sql",
    "description": "Run one read-only SQLite SELECT query on the LearnFlow database. Returns the column "
                   "names and rows, or an error message. At most 200 rows.",
    "parameters": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "One SQLite SELECT statement."}},
        "required": ["sql"],
    },
}


# ------------------------------------------------------------------ read-only SQL tool
def execute_readonly(db: Path, sql: str) -> dict:
    """Run one SELECT with read-only controls enforced by SQLite and Python, not by the prompt."""
    if not isinstance(sql, str) or not sql.strip():
        return {"error": "Empty query."}
    if len(sql) > MAX_SQL_CHARS:
        return {"error": f"Query longer than {MAX_SQL_CHARS} characters."}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # main file read-only; not a full sandbox
    try:
        con.execute("PRAGMA query_only = ON")  # also blocks writes to TEMP tables
        con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)  # ATTACH can't open a second file
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and (arg1 in ALLOWED_TABLES or not arg1):
                return sqlite3.SQLITE_OK
            if action == getattr(sqlite3, "SQLITE_RECURSIVE", 33):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY  # INSERT, UPDATE, DELETE, ATTACH, PRAGMA, CREATE, other tables

        con.set_authorizer(authorizer)
        deadline = time.monotonic() + MAX_QUERY_SECONDS
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur = con.execute(sql)  # sqlite3 rejects a second statement in the same string
        rows = cur.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            return {"error": f"More than {MAX_ROWS} rows. Aggregate the result or add a LIMIT."}
        out = {"columns": [d[0] for d in cur.description or []], "rows": [list(r) for r in rows]}
        if len(json.dumps(out, default=str)) > MAX_RESULT_CHARS:
            return {"error": f"Result larger than {MAX_RESULT_CHARS} characters."}
        return out
    except (sqlite3.Error, sqlite3.Warning) as exc:
        message = "Query took longer than 5 seconds and was stopped." if "interrupted" in str(exc) else str(exc)
        return {"error": f"{type(exc).__name__}: {message}"}
    finally:
        con.close()


# ------------------------------------------------------------------ helpers
def load_api_key() -> str | None:
    """Read only GEMINI_API_KEY from this project's .env file."""
    return (dotenv_values(HERE / ".env").get("GEMINI_API_KEY") or "").strip() or None


def usage_row(usage) -> dict:
    get = lambda name: int(getattr(usage, name, 0) or 0)  # noqa: E731
    row = {"input": get("total_input_tokens"), "cached": get("total_cached_tokens"),
           "output": get("total_output_tokens"), "thinking": get("total_thought_tokens"),
           "tool_use": get("total_tool_use_tokens"), "total": get("total_tokens")}
    # total_cached_tokens is the cached part of total_input_tokens, so price the two parts separately.
    row["cost"] = ((row["input"] - row["cached"]) * PRICE_INPUT + row["cached"] * PRICE_CACHED
                   + (row["output"] + row["thinking"]) * PRICE_OUTPUT) / 1_000_000
    return row


def ranking(parsed: dict | None) -> list[str]:
    return [] if not parsed else [ch for ch, _ in sorted(parsed.items(), key=lambda kv: -kv[1][2])]


def sql_matches_text(frozen_sql: str | None, text: str | None) -> bool:
    """Confirm the frozen executed SQL matches the final ```sql block in the model's answer."""
    if not frozen_sql or not text:
        return False
    blocks = re.findall(r"```sql\s*(.*?)```", text, flags=re.S | re.I)
    if not blocks:
        return False
    norm = lambda s: " ".join(s.split()).rstrip(";")  # noqa: E731
    return norm(frozen_sql) == norm(blocks[-1])


# ------------------------------------------------------------------ the run
def run_experiment(api_key: str | None = None) -> Iterator[dict]:
    """Yield progress events; the last one is {"type": "done", "record": ...}."""
    key = api_key or load_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")
    client = genai.Client(api_key=key)
    request = dict(model=MODEL, system_instruction=SYSTEM_INSTRUCTION, tools=[RUN_SQL],
                   generation_config={"thinking_level": THINKING_LEVEL})  # resent on every turn
    start = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "run_id": run_id, "model": MODEL, "thinking_level": THINKING_LEVEL,
        "sdk_version": genai.__version__, "python": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version, "db_sha256": hashlib.sha256(DB_PATH.read_bytes()).hexdigest(),
        "limits": {"requests": MAX_REQUESTS, "tool_calls": MAX_TOOL_CALLS, "tool_calls_per_phase":
                   MAX_TOOL_CALLS_PER_PHASE, "seconds": MAX_SECONDS, "spend_usd": MAX_SPEND_USD,
                   "corrections": MAX_CORRECTIONS},
        "prices_per_million": {"input": PRICE_INPUT, "cached_input": PRICE_CACHED, "output": PRICE_OUTPUT},
        "prompts": {"system": SYSTEM_INSTRUCTION, "original": ORIGINAL_TASK, "revision": REVISION_TASK},
        "phases": {}, "requests": [], "events": [], "stop": "finished",
    }
    tool_calls = 0

    def emit(event: dict) -> dict:
        event["t"] = round(time.monotonic() - start, 1)
        record["events"].append(event)
        return event

    def spent() -> float:
        return sum(r["cost"] for r in record["requests"])

    def run_phase(name: str, first_input, previous_id: str | None) -> Iterator[dict]:
        nonlocal tool_calls
        phase = {"input": first_input, "previous_interaction_id": previous_id, "interaction_ids": [],
                 "queries": [], "text": "", "final_interaction_id": None, "status": None}
        record["phases"][name] = phase
        yield emit({"type": "phase", "phase": name})
        inp, prev, phase_calls = first_input, previous_id, 0
        while True:
            if len(record["requests"]) >= MAX_REQUESTS:
                record["stop"] = "request limit"
            elif time.monotonic() - start >= MAX_SECONDS:
                record["stop"] = "time limit"
            elif spent() >= MAX_SPEND_USD:
                record["stop"] = "spend limit"
            if record["stop"] != "finished":
                yield emit({"type": "limit", "text": record["stop"]})
                return
            t0 = time.monotonic()
            kwargs = dict(input=inp, timeout=REQUEST_TIMEOUT_S, **request)
            if prev:
                kwargs["previous_interaction_id"] = prev
            try:
                interaction = client.interactions.create(**kwargs)
            except Exception as exc:  # report the API error without the key
                record["stop"] = f"API error: {type(exc).__name__}: {str(exc).replace(key, '***')[:300]}"
                yield emit({"type": "error", "text": record["stop"]})
                return
            row = {"n": len(record["requests"]) + 1, "phase": name, "interaction_id": interaction.id,
                   "status": interaction.status, "latency_s": round(time.monotonic() - t0, 1),
                   **usage_row(interaction.usage)}
            record["requests"].append(row)
            phase["interaction_ids"].append(interaction.id)
            yield emit({"type": "usage", **row, "running_cost": spent()})

            calls = [s for s in interaction.steps or [] if s.type == "function_call"]
            if interaction.status in ("failed", "cancelled", "incomplete") or not calls:
                phase.update(text=interaction.output_text or "", final_interaction_id=interaction.id,
                             status=interaction.status)
                yield emit({"type": "answer", "phase": name, "status": interaction.status})
                return
            results = []
            for call in calls:
                args = dict(call.arguments or {})
                if call.name != "run_sql":
                    out = {"error": f"Unknown tool {call.name}."}
                elif tool_calls >= MAX_TOOL_CALLS or phase_calls >= MAX_TOOL_CALLS_PER_PHASE:
                    out = {"error": "Tool-call limit reached. Answer with the results you already have."}
                else:
                    tool_calls, phase_calls = tool_calls + 1, phase_calls + 1
                    q0 = time.monotonic()
                    out = execute_readonly(DB_PATH, args.get("sql", ""))
                    phase["queries"].append({"sql": args.get("sql", ""), "seconds": round(time.monotonic() - q0, 3),
                                             **out})
                yield emit({"type": "tool_call", "phase": name, "sql": args.get("sql", ""),
                            "rows": len(out.get("rows", [])), "error": out.get("error")})
                results.append({"type": "function_result", "name": call.name, "call_id": call.id,
                                "is_error": "error" in out,
                                "result": [{"type": "text", "text": json.dumps(out, default=str)}]})
            inp, prev = results, interaction.id

    def freeze(name: str) -> tuple[str | None, tuple | None]:
        """The phase's answer is its last successful query that returns the required columns."""
        for q in reversed(record["phases"][name]["queries"]):
            if "rows" in q and reference.parse_result(q["columns"], q["rows"]) is not None:
                record["phases"][name]["frozen_sql"] = q["sql"]
                return q["sql"], (q["columns"], q["rows"])
        record["phases"][name]["frozen_sql"] = None
        return None, None

    # 1. Original enrollment-based analysis.
    yield from run_phase("original", ORIGINAL_TASK, None)
    original = record["phases"]["original"]
    sql, result = freeze("original")
    original["validation"] = reference.validate(sql, result, "enrollment", execute_readonly)
    original_parsed = reference.parse_result(*result) if result else None
    yield emit({"type": "frozen", "phase": "original", "found": sql is not None})
    for c in original["validation"]["checks"]:
        yield emit({"type": "check", "phase": "original", "name": c["name"], "passed": c["passed"]})

    # 2. The correction, chained to the finished original turn.
    revised_parsed, corrections = None, 0
    if record["stop"] == "finished" and original["final_interaction_id"]:
        yield from run_phase("revision", REVISION_TASK, original["final_interaction_id"])
        current = "revision"
        while True:
            sql, result = freeze(current)
            check = reference.validate(sql, result, "completion", execute_readonly, original=original_parsed)
            record["phases"][current]["validation"] = check
            revised_parsed = reference.parse_result(*result) if result else None
            yield emit({"type": "frozen", "phase": current, "found": sql is not None})
            for c in check["checks"]:
                yield emit({"type": "check", "phase": current, "name": c["name"], "passed": c["passed"]})
            last_id = record["phases"][current]["final_interaction_id"]
            if check["passed"] or corrections >= MAX_CORRECTIONS or record["stop"] != "finished" or not last_id:
                break
            corrections += 1
            message = reference.feedback(check["checks"])
            yield emit({"type": "correction", "failed": [c["name"] for c in check["checks"] if not c["passed"]]})
            yield from run_phase("correction", message, last_id)
            current = "correction"

    final_phase = "correction" if "correction" in record["phases"] else "revision"
    final_data = record["phases"].get(final_phase, {})
    final_check = final_data.get("validation", {})
    orig_completed = original.get("status") == "completed"
    rev_completed = record["phases"].get("revision", {}).get("status") == "completed"
    final_completed = final_data.get("status") == "completed"
    reqs = record["requests"]
    record.update({
        "elapsed_s": round(time.monotonic() - start, 1),
        "tool_calls": tool_calls,
        "reference": {"enrollment": original["validation"]["reference"],
                      "completion": final_check.get("reference"),
                      "sql_matches_python": {m: reference.reference_sql(DB_PATH, m) == reference.reference_python(DB_PATH, m)
                                             for m in ("enrollment", "completion")}},
        "outcome": {
            "original_matches_reference": orig_completed and original["validation"]["passed"],
            "revision_matches_reference": final_completed and bool(final_check.get("passed")),
            "first_revision_passed": rev_completed and bool(record["phases"].get("revision", {}).get("validation", {}).get("passed")),
            "frozen_matches_text_sql": {
                "original": sql_matches_text(original.get("frozen_sql"), original.get("text")),
                "final": sql_matches_text(final_data.get("frozen_sql"), final_data.get("text")),
            },
            "correction_used": corrections > 0,
            "original_ranking": ranking(original_parsed),
            "revised_ranking": ranking(revised_parsed),
        },
        "results": {"original": original_parsed, "revised": revised_parsed},
        "cost": {"requests": len(reqs), **{k: sum(r[k] for r in reqs) for k in
                 ("input", "cached", "output", "thinking", "tool_use", "total", "cost")}},
    })
    o = record["outcome"]
    o["order_changed"] = bool(o["original_ranking"] and o["revised_ranking"]
                              and o["original_ranking"] != o["revised_ranking"])
    o["failed"] = not o["revision_matches_reference"]
    RUNS.mkdir(exist_ok=True)
    (RUNS / f"run_{run_id}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    yield {"type": "done", "record": record, "t": record["elapsed_s"]}


if __name__ == "__main__":
    for event in run_experiment():
        kind = event["type"]
        if kind == "done":
            r = event["record"]
            print(f"\nSaved runs/run_{r['run_id']}.json | {r['elapsed_s']} s | {r['cost']['requests']} requests | "
                  f"{r['tool_calls']} tool calls | ${r['cost']['cost']:.4f} | outcome: {r['outcome']}")
        elif kind == "usage":
            print(f"[{event['t']:>6}s] request {event['n']} ({event['phase']}): {event['status']}, "
                  f"in {event['input']:,} (cached {event['cached']:,}), out {event['output']:,}, "
                  f"thinking {event['thinking']:,}, {event['latency_s']} s, ${event['cost']:.4f}")
        elif kind == "tool_call":
            sql = " ".join(event["sql"].split())
            print(f"[{event['t']:>6}s]   run_sql -> {event['error'] or str(event['rows']) + ' rows'}: {sql[:160]}")
        elif kind == "check":
            print(f"[{event['t']:>6}s]   check {event['phase']}/{event['name']}: {'pass' if event['passed'] else 'FAIL'}")
        else:
            print(f"[{event['t']:>6}s] {kind}: {({k: v for k, v in event.items() if k not in ('type', 't')})}")
