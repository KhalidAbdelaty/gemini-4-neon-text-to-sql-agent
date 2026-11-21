"""Streamlit front end for the LearnFlow SQL analyst.

    streamlit run app_streamlit.py

"Run the analysis" starts a live run against the Gemini API, billed to GEMINI_API_KEY in .env.
"Show the latest recorded run" opens the newest runs/run_*.json and makes no API call.
"""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image  # installed with Streamlit

from agent import (MAX_CORRECTIONS, MAX_REQUESTS, MAX_SECONDS, MAX_SPEND_USD, MAX_TOOL_CALLS,
                   MAX_TOOL_CALLS_PER_PHASE, MODEL, PRICE_CACHED, PRICE_INPUT, PRICE_OUTPUT, THINKING_LEVEL,
                   load_api_key, run_experiment)
from reference import REFERENCE_SQL

HERE = Path(__file__).resolve().parent
ASSETS, RUNS = HERE / "assets", HERE / "runs"

# Google palette (Partner Marketing Hub). Fills that carry white text are darkened with black until the
# pair measures at least 4.5:1 (WCAG AA for normal text).
BLUE_FILL = "#3974D4"   # source swatch Blue 500 #4285F4 (3.56:1 with white), 13% black: 4.54:1
BLUE_HOVER = "#3264B7"  # Blue 500 with 25% black: 5.9:1 with white
RED_FILL = "#D73E31"    # source swatch Red 500 #EA4335 (3.92:1 with white), 8% black: 4.54:1
GREEN_FILL = "#2A8642"  # source swatch Green 500 #34A853 (3.06:1 with white), 20% black: 4.58:1
YELLOW = "#FBBC05"      # Yellow 500 keeps black text: 12.3:1
GREY_700 = "#5F6368"    # secondary text on white: 6.05:1
BORDER = "rgba(154, 160, 166, 0.4)"  # Grey 500 #9AA0A6 at 40% opacity, borders only

FEATURES = [
    (":material/forum:", "Interactions API",
     "Every turn is one client.interactions.create() call to gemini-4-neon at the high thinking level."),
    (":material/table_view:", "One read-only SQL tool",
     "run_sql opens the database with mode=ro, query_only, an authorizer, a time limit, and a 200-row cap."),
    (":material/link:", "Previous interaction id",
     "The correction continues the finished first analysis. Tools, instruction, and config are resent."),
    (":material/rule:", "Reference SQL",
     "Written before the first request and never shown to Neon. Python also checks the frozen query on probes."),
    (":material/replay:", "One correction cap",
     "If a check fails, Neon gets the failing check names, not the expected counts, and one more try."),
    (":material/payments:", "Usage-based cost",
     "Cost comes from each response's usage object: uncached input, cached input, output, and thinking."),
]

st.set_page_config(page_title="LearnFlow Activation", page_icon=":material/database:", layout="wide")
st.logo(str(ASSETS / "datacamp-logo.png"), link="https://www.datacamp.com", size="large")

st.markdown(
    f"""
    <style>
      .stApp, [data-testid="stSidebar"] {{ background: #FFFFFF; color: #000000;
        font-family: "Google Sans", "Product Sans", Arial, sans-serif; }}
      .stApp::before {{ content: ""; position: fixed; top: 0; left: 0; right: 0; height: 3px; z-index: 1000000;
        background: linear-gradient(90deg, #EA4335, #FBBC05, #34A853, #4285F4); }}
      .block-container {{ padding-top: 4.5rem; max-width: 1180px; }}  /* clear the 60px header */
      [data-testid="stHeader"] {{ background: transparent; }}  /* keep the brand row visible under the toolbar */
      .stApp :is(p, li, h1, h2, h3, h4, label, button) {{
        font-family: "Google Sans", "Product Sans", Arial, sans-serif; }}
      h1, h2, h3, h4 {{ color: #000000; }}
      [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {GREY_700}; opacity: 1; }}
      [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"] details {{
        border-color: {BORDER} !important; border-radius: .6rem; background: #FFFFFF; }}
      [data-testid="stBaseButton-primary"] {{ background: {BLUE_FILL}; border-color: {BLUE_FILL}; color: #FFFFFF; }}
      [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primary"]:focus {{
        background: {BLUE_HOVER}; border-color: {BLUE_HOVER}; color: #FFFFFF; }}
      [data-testid="stBaseButton-secondary"] {{ background: #FFFFFF; color: #000000; border: 1px solid {BORDER}; }}
      [data-testid="stBaseButton-secondary"]:hover {{ border-color: {BLUE_FILL}; color: #000000; }}
      [data-testid="stTab"][aria-selected="true"], [data-testid="stTab"][aria-selected="true"] p {{
        color: {BLUE_FILL}; }}
      [role="tablist"] .react-aria-SelectionIndicator {{ background-color: {BLUE_FILL} !important;
        border-color: {BLUE_FILL} !important; }}
      [data-testid="stMetricValue"], [data-testid="stMetricValue"] p {{ color: #000000;
        font-family: ui-monospace, Menlo, Consolas, monospace; }}
      [data-testid="stMetricLabel"] p, [data-testid="stMetricDelta"] {{ color: {GREY_700}; opacity: 1; }}
      /* SQL highlighting in Google colors that pass 4.5:1 on the white code background */
      [data-testid="stCode"] pre {{ background: #FFFFFF !important; border: 1px solid {BORDER}; border-radius: .6rem; }}
      [data-testid="stCode"] code {{ color: #000000; }}
      [data-testid="stCode"] .token:is(.keyword, .function, .builtin) {{ color: {BLUE_FILL} !important; }}
      [data-testid="stCode"] .token:is(.string, .number, .boolean) {{ color: {GREEN_FILL} !important; }}
      [data-testid="stCode"] .token.operator {{ color: {RED_FILL} !important; }}
      [data-testid="stCode"] .token:is(.punctuation, .comment) {{ color: {GREY_700} !important; }}
      .brand {{ display: flex; align-items: center; gap: .9rem; flex-wrap: wrap; margin-bottom: .5rem; }}
      .brand img {{ height: 34px; width: auto; display: block; }}
      .mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; }}
      .model {{ display: inline-flex; align-items: center; box-sizing: border-box; height: 34px;
        padding: 0 .8rem; font-size: .95rem; line-height: 1; white-space: nowrap; background: #FFFFFF;
        color: #000000; border: 1px solid {BORDER}; border-radius: .6rem; }}
      .pill {{ display: inline-block; border-radius: 999px; padding: .2rem .8rem; font-weight: 700;
        font-size: .9rem; margin: 0 .4rem .4rem 0; }}
      .pill-ok {{ background: {GREEN_FILL}; color: #FFFFFF; }}
      .pill-warn {{ background: {YELLOW}; color: #000000; }}
      .pill-bad {{ background: {RED_FILL}; color: #FFFFFF; }}
      .pill-neutral {{ background: #FFFFFF; color: #000000; border: 1px solid {BORDER}; }}
      .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .6rem;
        margin: .6rem 0 1rem; }}
      .stat {{ background: #FFFFFF; border: 1px solid {BORDER}; border-radius: .6rem; padding: .55rem .8rem; }}
      .stat .label {{ font-size: .78rem; color: {GREY_700}; }}
      .stat .value {{ font-size: 1.25rem; font-weight: 700; color: #000000;
        font-family: ui-monospace, Menlo, Consolas, monospace; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def trimmed_png_b64(path: str) -> str:
    """The Gemini PNG has wide transparent margins; crop them so the mark fills its height."""
    image = Image.open(path).convert("RGBA")
    buffer = io.BytesIO()
    image.crop(image.getchannel("A").getbbox()).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def pill(text: str, kind: str) -> str:
    return f"<span class='pill pill-{kind}'>{html.escape(text)}</span>"


def stats_html(items: list[tuple[str, str]]) -> str:
    cells = "".join(f"<div class='stat'><div class='label'>{html.escape(label)}</div>"
                    f"<div class='value'>{html.escape(value)}</div></div>" for label, value in items)
    return f"<div class='stats'>{cells}</div>"


def one_line(text: str, limit: int = 150) -> str:
    text = " ".join((text or "").split()).replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3] + "..."


st.markdown(
    f"""<div class="brand">
      <img src="data:image/png;base64,{trimmed_png_b64(str(ASSETS / 'gemini-logo.png'))}" alt="Gemini logo"/>
      <span class="model mono">{html.escape(MODEL)}</span>
    </div>""",
    unsafe_allow_html=True,
)
st.title("LearnFlow Activation")
st.markdown("Neon revises LearnFlow's activation metric, and Python checks the SQL.")

# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("Run settings")
    st.markdown(f"- Model: `{MODEL}`", unsafe_allow_html=False)
    st.markdown(f"- Thinking level: {pill(THINKING_LEVEL, 'neutral')}", unsafe_allow_html=True)
    st.markdown(
        f"- Requests: at most {MAX_REQUESTS}\n"
        f"- Tool calls: at most {MAX_TOOL_CALLS} ({MAX_TOOL_CALLS_PER_PHASE} per turn chain)\n"
        f"- Time: {MAX_SECONDS // 60} minutes\n"
        f"- Spend: no new request after ${MAX_SPEND_USD:.2f}\n"
        f"- Corrections: {MAX_CORRECTIONS} after a failed check"
    )
    st.caption("Limits are checked by the application before every request.")
    st.subheader("Price per million tokens")
    st.markdown(
        "Gemini Developer API, paid Standard tier (checked November 21, 2026):\n"
        f"- Input: ${PRICE_INPUT:.2f}\n"
        f"- Output, including thinking: ${PRICE_OUTPUT:.2f}\n"
        f"- Cached input: ${PRICE_CACHED:.2f}"
    )
    st.caption("Gemini 4 Neon is not available on the free tier.")
    if st.session_state.get("spend"):
        st.metric("Spent this session", f"${st.session_state.spend:.4f}")

# ---------------------------------------------------------------- features
st.subheader("What this run uses")
cols = st.columns(2)
for i, (icon, name, desc) in enumerate(FEATURES):
    with cols[i % 2], st.container(border=True):
        st.markdown(f"{icon} **{name}**")
        st.caption(desc)

# --------------------------------------------------------------- controls
st.session_state.setdefault("running", False)


def start_run() -> None:
    if load_api_key():
        st.session_state.running = True
    else:
        st.session_state.key_missing = True


st.write("")
left, right = st.columns(2)
with left:
    st.button("Run the analysis", type="primary", icon=":material/play_arrow:", width="stretch",
              on_click=start_run, disabled=st.session_state.running,
              help="Starts a live run that calls the Gemini API.")
with right:
    recorded = st.button("Show the latest recorded run", icon=":material/history:", width="stretch",
                         disabled=st.session_state.running,
                         help="Opens the newest file in runs/ without calling the API.")
st.caption("A live run calls the Gemini API and is billed to GEMINI_API_KEY in .env.")

if st.session_state.pop("key_missing", False):
    st.error("GEMINI_API_KEY is missing from .env. Add the line GEMINI_API_KEY=your-key to the .env file "
             "in this folder, then start the run again.")


# ----------------------------------------------------------------- logging
class Counters:
    def __init__(self):
        self.t, self.phase, self.requests, self.tools, self.cost = 0.0, "original", 0, 0, 0.0

    def strip(self) -> str:
        return stats_html([("Elapsed", f"{self.t:.1f} s"), ("Phase", self.phase), ("API requests", str(self.requests)),
                           ("Tool calls", str(self.tools)), ("Running cost", f"${self.cost:.4f}")])


def log_event(log, e: dict, c: Counters) -> None:
    """One line per event. Shows Neon's SQL and check names, never reference values or the key."""
    stamp, kind = f"`{e['t']:>5.1f}s`", e["type"]
    c.t = e["t"]
    if kind == "phase":
        c.phase = e["phase"]
        log.markdown(f"{stamp} :material/flag: **{e['phase']}**")
    elif kind == "usage":
        c.requests, c.cost = e["n"], e["running_cost"]
        log.markdown(f"{stamp} :material/memory: request {e['n']} · {e['status']} · {e['input']:,} in "
                     f"({e['cached']:,} cached) · {e['output']:,} out · {e['thinking']:,} thinking · {e['latency_s']} s")
    elif kind == "tool_call":
        c.tools += 1
        result = f":red-badge[error] {one_line(e['error'], 120)}" if e["error"] else f"{e['rows']} row{'' if e['rows'] == 1 else 's'}"
        log.markdown(f"{stamp} :blue-badge[run_sql] `{one_line(e['sql'])}` → {result}")
    elif kind == "answer":
        log.markdown(f"{stamp} :material/chat: Neon answered ({e['status']})")
    elif kind == "frozen":
        log.markdown(f"{stamp} :material/lock: {e['phase']} query frozen" if e["found"]
                     else f"{stamp} :red-badge[missing] no query with the required columns in {e['phase']}")
    elif kind == "check":
        badge = ":green-badge[pass]" if e["passed"] else ":red-badge[fail]"
        log.markdown(f"{stamp} {badge} {e['phase']} · {e['name']}")
    elif kind == "correction":
        log.markdown(f"{stamp} :orange-badge[correction] sent failing check names: {', '.join(e['failed'])}")
    elif kind in ("limit", "error"):
        log.markdown(f"{stamp} :red-badge[stopped] {one_line(e['text'], 200)}")


# ----------------------------------------------------------------- results
def fit(rows: int) -> int:
    """Pixel height that shows every row without an inner scroll bar."""
    return 38 + 35 * rows + 2


def rate_table(results: dict | None) -> pd.DataFrame:
    rows = [{"Channel": ch, "Eligible": v[0], "Activated": v[1], "Rate": round(v[2], 4)}
            for ch, v in (results or {}).items()]
    return pd.DataFrame(rows)


def render_run(record: dict) -> None:
    o, c, phases = record["outcome"], record["cost"], record["phases"]
    st.divider()
    with st.status(f"Run log ({record['stop']}), replayed from the record", state="complete", expanded=False):
        log, counters = st.container(height=420, border=True), Counters()
        for event in record["events"]:
            log_event(log, event, counters)
    st.caption(f"Run {record['run_id']} · google-genai {record['sdk_version']} · Python {record['python']} · "
               f"SQLite {record['sqlite_version']} · stop: {record['stop']}")
    st.markdown(
        pill(f"reference match: {'yes' if o['revision_matches_reference'] else 'no'}",
             "ok" if o["revision_matches_reference"] else "neutral")
        + pill(f"correction used: {'yes' if o['correction_used'] else 'no'}",
               "warn" if o["correction_used"] else "neutral")
        + pill(f"failed: {'yes' if o['failed'] else 'no'}", "bad" if o["failed"] else "neutral")
        + pill(f"thinking: {record['thinking_level']}", "neutral"),
        unsafe_allow_html=True)
    st.markdown(stats_html([
        ("Wall-clock time", f"{record['elapsed_s']:.1f} s"),
        ("API requests", str(c["requests"])),
        ("Tool calls", str(record["tool_calls"])),
        ("Correction attempts", "1" if o["correction_used"] else "0"),
        ("Channel order changed", "yes" if o["order_changed"] else "no"),
        ("Cost", f"${c['cost']:.4f}"),
    ]), unsafe_allow_html=True)

    tabs = st.tabs([":material/description: Report", ":material/code: SQL", ":material/bar_chart: Channels",
                    ":material/verified: Reference", ":material/payments: Cost"])
    with tabs[0]:
        st.caption("What Neon said. These are the model's words, not the check.")
        for name, label in (("original", "Original analysis (enrollment)"), ("revision", "Revised analysis (lesson completion)"),
                            ("correction", "Correction turn")):
            if name in phases:
                with st.container(border=True):
                    st.markdown(f"**{label}**")
                    st.markdown(phases[name]["text"] or "_No answer text._")
    with tabs[1]:
        left, right = st.columns(2)
        final = "correction" if "correction" in phases else "revision"
        for col, name, label in ((left, "original", "Original query"), (right, final, "Revised query")):
            with col:
                st.markdown(f"**{label}**")
                sql = phases.get(name, {}).get("frozen_sql")
                st.code(sql or "-- no frozen query", language="sql", wrap_lines=True)
        st.caption("Each frozen query is the last successful run_sql call that returned the required columns.")
    with tabs[2]:
        res = record["results"]
        left, right = st.columns(2)
        with left:
            st.markdown("**Enrollment within 7 days**")
            st.dataframe(rate_table(res.get("original")), hide_index=True, width="stretch")
        with right:
            st.markdown("**Lesson completed within 7 days**")
            st.dataframe(rate_table(res.get("revised")), hide_index=True, width="stretch")
        st.caption(f"Rows as Neon's frozen queries returned them. Order before: {', '.join(o['original_ranking'])}. "
                   f"Order after: {', '.join(o['revised_ranking'])}. Synthetic data.")
    with tabs[3]:
        revision = phases.get("correction") or phases.get("revision") or {}
        if not revision.get("validation"):
            st.info("The reference stays empty until the revised query is frozen.")
        else:
            with st.container(border=True):
                st.markdown(":material/shield: **Harness only. Nothing in this tab was sent to Neon.**")
                ref = record["reference"]
                left, right = st.columns(2)
                for col, metric, label in ((left, "enrollment", "Reference: enrollment"),
                                           (right, "completion", "Reference: lesson completion")):
                    with col:
                        st.markdown(f"**{label}**")
                        st.dataframe(pd.DataFrame([{"Channel": ch, "Eligible": v["eligible"], "Activated": v["activated"],
                                                    "Rate": v["rate"]} for ch, v in (ref.get(metric) or {}).items()]),
                                     hide_index=True, width="stretch")
                checks = [{"Query": name, "Check": ch["name"], "Result": "Pass" if ch["passed"] else "Fail"}
                          for name in ("original", "revision", "correction") if name in phases
                          for ch in phases[name].get("validation", {}).get("checks", [])]
                st.dataframe(pd.DataFrame(checks), hide_index=True, width="stretch", height=fit(len(checks)))
                st.caption("Reference SQL and plain-Python reference agree: "
                           + ", ".join(f"{m} {'yes' if ok else 'no'}" for m, ok in ref["sql_matches_python"].items()))
                with st.expander("Reference SQL"):
                    for metric, sql in REFERENCE_SQL.items():
                        st.code(sql.strip(), language="sql", wrap_lines=True)
    with tabs[4]:
        uncached = c["input"] - c["cached"]
        k = st.columns(4)
        k[0].metric("Uncached input", f"{uncached:,}", f"${uncached * PRICE_INPUT / 1e6:.4f}", delta_color="off", delta_arrow="off")
        k[1].metric("Cached input", f"{c['cached']:,}", f"${c['cached'] * PRICE_CACHED / 1e6:.4f}", delta_color="off", delta_arrow="off")
        k[2].metric("Output", f"{c['output']:,}", f"${c['output'] * PRICE_OUTPUT / 1e6:.4f}", delta_color="off", delta_arrow="off")
        k[3].metric("Thinking", f"{c['thinking']:,}", f"${c['thinking'] * PRICE_OUTPUT / 1e6:.4f}", delta_color="off", delta_arrow="off")
        st.dataframe(pd.DataFrame(record["requests"])[["n", "phase", "status", "latency_s", "input", "cached",
                                                       "output", "thinking", "cost"]].round({"cost": 4}),
                     hide_index=True, width="stretch", height=fit(len(record["requests"])))
        st.caption("All figures come from the usage object on each response, priced at the paid Standard rates "
                   "in the sidebar.")


def latest_run() -> dict | None:
    paths = sorted(RUNS.glob("run_*.json"))
    if paths:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    sample = RUNS / "example_run.json"
    return json.loads(sample.read_text(encoding="utf-8")) if sample.exists() else None


# ----------------------------------------------------------------- actions
if st.session_state.running:
    counters, record = Counters(), None
    strip = st.empty()
    strip.markdown(counters.strip(), unsafe_allow_html=True)
    try:
        with st.status("Running the LearnFlow analysis...", expanded=True) as status:
            log = st.container(height=420, border=True)
            for event in run_experiment():
                if event["type"] == "done":
                    record = event["record"]
                    counters.t, counters.cost = record["elapsed_s"], record["cost"]["cost"]
                    status.update(label=f"Run finished ({record['stop']})", state="complete", expanded=False)
                else:
                    log_event(log, event, counters)
                strip.markdown(counters.strip(), unsafe_allow_html=True)
    except Exception as exc:  # show the problem without a traceback or the key
        st.error(f"The run stopped: {type(exc).__name__}. Check the key, billing, and network, then try again.")
    st.session_state.running = False
    if record is not None:
        st.session_state.spend = st.session_state.get("spend", 0.0) + record["cost"]["cost"]
        st.session_state.record = record
    st.rerun()

if recorded:
    st.session_state.record = latest_run()
    if st.session_state.record is None:
        st.info("No recorded runs yet. Start a live run, or run python agent.py, and the result will appear here.")

if st.session_state.get("record"):
    render_run(st.session_state.record)
