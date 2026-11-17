# -*- coding: utf-8 -*-
"""One entry point shared by the CLI, the web UI and the REST API."""
import json
import os
import secrets
import threading
import time
import uuid

from .agent import MODEL, Agent, Cancelled  # noqa: F401 - Cancelled is re-exported for the web UI
from .db import Store

MAX_QUESTION_CHARS = 500
DATA_DIR = os.environ.get("VLR_DATA_DIR", "output")
DB_PATH = os.environ.get("VLR_DB", os.path.join(DATA_DIR, "medical_cache.db"))


def run_review(question, n_papers=3, language="he", model=MODEL, effort="medium", on_event=None,
               client=None, data_dir=None, db_path=None, should_stop=None):
    """Run the agent once. Returns the agent's result dict plus the report Markdown."""
    if not question or not question.strip():
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters")
    if not 1 <= int(n_papers) <= 5:
        raise ValueError("n_papers must be between 1 and 5")
    data_dir = data_dir or DATA_DIR
    # unique per run: two users starting in the same second must not overwrite each other's report
    run_dir = os.path.join(data_dir, "runs", f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    os.makedirs(run_dir, exist_ok=True)
    store = Store(db_path or DB_PATH)
    try:
        agent = Agent(store, os.path.join(data_dir, "pdfs"), client=client, model=model, effort=effort,
                      on_event=on_event, should_stop=should_stop)
        result = agent.run(question.strip(), n_papers=int(n_papers), language=language,
                           out_path=os.path.join(run_dir, "Research_Summary_Report.md"))
    finally:
        store.close()
    with open(result["report"], encoding="utf-8") as f:
        result["report_markdown"] = f.read()
    return result


# ------------------------------------------------------------------ background runs (web UI)
# A review takes minutes. On a phone the screen locks or the browser drops the connection, and the
# page's own script is stopped with it - the money is spent but the report is lost. So the web UI runs
# the review in a thread that does not belong to the page, and keeps the result here under a random
# token that the page puts in its URL; a reconnecting page finds the run again by that token.
_JOBS = {}
_JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 6 * 3600


def start_job(question, n_papers=3, language="he", model=MODEL, **kw):
    """Start run_review in a background thread. Returns the token for get_job."""
    token = secrets.token_urlsafe(16)
    job = {"events": [], "result": None, "error": None, "done": False, "cancel": False, "started": time.time()}

    def on_event(kind, msg):
        job["events"].append((kind, msg))
        if kind in ("timing", "fetch", "search", "verify", "tool_error"):     # also in the host's log (Streamlit "Manage app")
            print(f"{time.strftime('%H:%M:%S')} [{token[:6]}] {kind}: {msg}", flush=True)

    def work():
        try:
            job["result"] = run_review(question, n_papers, language, model=model, on_event=on_event,
                                       should_stop=lambda: job["cancel"], **kw)
        except Exception as e:  # noqa: BLE001 - handed to the page, which decides what to show
            job["error"] = e
        finally:
            job["done"] = True

    with _JOBS_LOCK:
        now = time.time()
        for old in [t for t, j in _JOBS.items() if now - j["started"] > JOB_TTL_SECONDS]:
            del _JOBS[old]
        _JOBS[token] = job
    threading.Thread(target=work, daemon=True).start()
    return token


def cancel_job(token):
    """Ask a background run to stop before its next model call."""
    job = get_job(token)
    if job is not None:
        job["cancel"] = True


def get_job(token):
    """The background run for this token, or None (unknown token, or the app was restarted)."""
    with _JOBS_LOCK:
        return _JOBS.get(token)


def get_run(run_id, db_path=None):
    """A past run with every quote check (the audit trail), or None."""
    store = Store(db_path or DB_PATH)
    try:
        run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            return None
        checks = store.conn.execute(
            "SELECT q.paper_id, p.title, q.claimed_page, q.text, q.verified, q.found_on, q.reason "
            "FROM quote_checks q LEFT JOIN papers p USING (paper_id) WHERE q.run_id=? ORDER BY q.rowid",
            (run_id,)).fetchall()
        out = dict(run)
        out["quote_checks"] = [{**dict(c), "verified": bool(c["verified"]), "found_on": json.loads(c["found_on"] or "[]")}
                               for c in checks]
        out["summary"] = store.run_summary(run_id)
        return out
    finally:
        store.close()


def runs_since(timestamp, db_path=None):
    """Number of runs started after `timestamp` (used for the daily budget limit)."""
    store = Store(db_path or DB_PATH)
    try:
        return store.conn.execute("SELECT COUNT(*) FROM runs WHERE started_at > ?", (timestamp,)).fetchone()[0]
    finally:
        store.close()


def list_runs(limit=20, db_path=None):
    store = Store(db_path or DB_PATH)
    try:
        rows = store.conn.execute(
            "SELECT r.run_id, r.question, r.model, r.started_at, r.finished_at, "
            "COUNT(q.rowid) AS quotes, COALESCE(SUM(q.verified), 0) AS verified "
            "FROM runs r LEFT JOIN quote_checks q USING (run_id) GROUP BY r.run_id "
            "ORDER BY r.run_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        store.close()
