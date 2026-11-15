# -*- coding: utf-8 -*-
"""One entry point shared by the CLI, the web UI and the REST API."""
import json
import os
import time
import uuid

from .agent import MODEL, Agent
from .db import Store

MAX_QUESTION_CHARS = 500
DATA_DIR = os.environ.get("VLR_DATA_DIR", "output")
DB_PATH = os.environ.get("VLR_DB", os.path.join(DATA_DIR, "medical_cache.db"))


def run_review(question, n_papers=3, language="he", model=MODEL, effort="medium", on_event=None,
               client=None, data_dir=None, db_path=None):
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
                      on_event=on_event)
        result = agent.run(question.strip(), n_papers=int(n_papers), language=language,
                           out_path=os.path.join(run_dir, "Research_Summary_Report.md"))
    finally:
        store.close()
    with open(result["report"], encoding="utf-8") as f:
        result["report_markdown"] = f.read()
    return result


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
