# Repository Instructions

## Project
- This is a Python 3.9+ standard-library prototype for simulated banking workflows, analytics, risk controls, and a web UI.
- All account, balance, transaction, card, and investment activity is demo data. Do not describe or modify it as a real banking or payment integration unless the code explicitly supports that.
- The web client is the single file `web/index.html`; the Python application code is under `finagent/`.

## Structure
- `finagent/agent/`, `nlu/`, `tools/`, and `risk/` implement conversation handling, intent parsing, tool execution, and controls.
- `finagent/api/` and `run_api.py` provide the HTTP API; `finagent/analytics/`, `data/`, and `evaluation/` cover analytics, data access, and evaluation.
- `config.py` owns environment-driven configuration. Keep secrets in environment variables; never commit them or place them in fixtures, logs, or UI output.
- `tests/` contains the `unittest` suite. `scripts/` contains evaluation, audit, data, and UI-check utilities.
- Treat `runtime_data/`, `runtime_uploads/`, and `ui-artifacts*/` as generated/local state unless a task explicitly targets them.

## Development
- Prefer the standard library and existing patterns; `requirements.txt` documents optional dependencies rather than required runtime packages.
- Run the full test suite with `python -m unittest discover -s tests -q`.
- Check Python syntax with `python -m compileall -q finagent run_api.py scripts`.
- Run focused tests while iterating, then the full suite when practical. Keep test data deterministic and isolated from real user data.
- Start the web API locally with `python run_api.py --host 127.0.0.1 --port 8000`.

## Safety and correctness
- Preserve confirmation, verification, idempotency, rollback, and data-quality gates around simulated financial actions.
- Do not silently relax risk constraints or claim that estimates, public market data, OCR, or simulated results are guaranteed or real-time.
- Keep sensitive values masked in conversation history, API responses, logs, and tests.
- Make focused changes and update relevant tests and documentation when behavior or documented commands change.