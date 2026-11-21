# Gemini 4 Neon text-to-SQL agent: LearnFlow activation

A small Python project that asks Gemini 4 Neon (`gemini-4-neon`) to measure learner activation for a fictional
learning platform, LearnFlow, then changes the definition of activation and checks whether the revised SQL
still answers the right question. Neon writes the SQL. Python runs it through one read-only SQLite tool and
checks the revised query against a reference the model never sees.

All data is synthetic.

## Files

- `build_data.py` builds `data/learnflow.db` (1,200 learners, fixed seed)
- `agent.py` runs the experiment on the Interactions API and saves `runs/run_<UTC time>.json`
- `reference.py` holds the reference SQL, a plain-Python cross-check, and the validator
- `app_streamlit.py` shows a live or recorded run

## Run it

You need Python 3.11 or newer and a Gemini API key from a paid Google AI Studio project.
Gemini 4 Neon is not available on the free tier.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS or Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in this folder with one line:

```
GEMINI_API_KEY=your-key
```

Then:

```bash
python build_data.py     # optional: rebuilds data/learnflow.db from the seed
python reference.py      # prints both reference tables, no API call
python agent.py          # one live run, billed to your key
streamlit run app_streamlit.py
```

In the app, "Run the analysis" starts a live run and "Show the latest recorded run" opens the newest file in
`runs/` without calling the API.

Tested with `google-genai` 3.6.1, Python 3.11.9, and SQLite 3.45.1 on November 21, 2026.
