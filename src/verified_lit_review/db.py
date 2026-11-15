# -*- coding: utf-8 -*-
"""
SQLite storage: search cache, papers, page text (full-text searchable) and an audit trail.

One local file (default: medical_cache.db) holds everything the agent touches, so that
  * a question asked twice is answered from the cache, without hitting the network;
  * page passages can be retrieved with SQL full-text search (FTS5) - the "R" in RAG;
  * every quote the model proposed is logged with its verdict - an audit trail.

    from verified_lit_review.db import Store
    store = Store("medical_cache.db")
"""
import hashlib
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    source      TEXT NOT NULL,              -- e.g. 'europepmc'
    query_key   TEXT NOT NULL,              -- normalized query + params
    results     TEXT NOT NULL,              -- JSON list of paper dicts
    created_at  REAL NOT NULL,
    PRIMARY KEY (source, query_key)
);

CREATE TABLE IF NOT EXISTS papers (
    paper_id    TEXT PRIMARY KEY,           -- DOI if known, else pmcid / source id
    doi         TEXT,
    pmcid       TEXT,
    title       TEXT NOT NULL,
    authors     TEXT,
    journal     TEXT,
    year        TEXT,
    abstract    TEXT,
    pdf_urls    TEXT,                       -- JSON list of candidate open-access PDF links
    pdf_path    TEXT,                       -- local file once downloaded
    pdf_sha256  TEXT,                       -- fingerprint of the exact file that was verified
    source      TEXT,
    added_at    REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    paper_id UNINDEXED,
    page     UNINDEXED,                     -- 1-based PDF page number
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    model       TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    report_path TEXT,
    access_code TEXT                        -- which visitor ran it (web UI access codes)
);

CREATE TABLE IF NOT EXISTS quote_checks (
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    paper_id      TEXT NOT NULL,
    claimed_page  INTEGER,
    text          TEXT NOT NULL,
    verified      INTEGER NOT NULL,         -- 1 = found on the cited page
    found_on      TEXT,                     -- JSON list of pages where it really is
    reason        TEXT,
    checked_at    REAL NOT NULL
);
"""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    """Thin wrapper around one SQLite file. Safe to open many times; the schema is idempotent."""

    def __init__(self, path="medical_cache.db"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "access_code" not in cols:                       # migrate databases created before v0.2.1
            self.conn.execute("ALTER TABLE runs ADD COLUMN access_code TEXT")

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------ search cache
    def get_cached_search(self, source, query_key, max_age_days=30):
        row = self.conn.execute(
            "SELECT results, created_at FROM search_cache WHERE source=? AND query_key=?",
            (source, query_key)).fetchone()
        if row and time.time() - row["created_at"] <= max_age_days * 86400:
            return json.loads(row["results"])
        return None

    def put_cached_search(self, source, query_key, results):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO search_cache VALUES (?, ?, ?, ?)",
                (source, query_key, json.dumps(results, ensure_ascii=False), time.time()))

    # ------------------------------------------------------------ papers
    def upsert_paper(self, p):
        """Insert or update a paper dict (keys as returned by sources.search_*). Returns paper_id."""
        paper_id = p.get("doi") or p.get("pmcid") or p["source_id"]
        with self.conn:
            self.conn.execute(
                """INSERT INTO papers (paper_id, doi, pmcid, title, authors, journal, year, abstract,
                                       pdf_urls, source, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(paper_id) DO UPDATE SET
                       abstract = COALESCE(excluded.abstract, papers.abstract),
                       pdf_urls = excluded.pdf_urls""",
                (paper_id, p.get("doi"), p.get("pmcid"), p["title"], p.get("authors"), p.get("journal"),
                 p.get("year"), p.get("abstract"), json.dumps(p.get("pdf_urls", [])), p.get("source"),
                 time.time()))
        return paper_id

    def get_paper(self, paper_id):
        row = self.conn.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["pdf_urls"] = json.loads(d["pdf_urls"] or "[]")
        return d

    def set_pdf(self, paper_id, pdf_path):
        with self.conn:
            self.conn.execute("UPDATE papers SET pdf_path=?, pdf_sha256=? WHERE paper_id=?",
                              (pdf_path, sha256_file(pdf_path), paper_id))

    # ------------------------------------------------------------ page text (RAG retrieval)
    def add_pages(self, paper_id, page_texts):
        """Store the text of every PDF page (list index 0 = page 1). Replaces earlier text."""
        with self.conn:
            self.conn.execute("DELETE FROM pages WHERE paper_id=?", (paper_id,))
            self.conn.executemany("INSERT INTO pages (paper_id, page, text) VALUES (?, ?, ?)",
                                  [(paper_id, i, t) for i, t in enumerate(page_texts, 1)])

    def page_count(self, paper_id):
        return self.conn.execute("SELECT COUNT(*) FROM pages WHERE paper_id=?", (paper_id,)).fetchone()[0]

    def search_pages(self, paper_id, query, k=5):
        """Top-k pages of one paper for a free-text query, ranked by FTS5 BM25."""
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 2]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = self.conn.execute(
            "SELECT page, text, bm25(pages) AS score FROM pages "
            "WHERE pages MATCH ? AND paper_id=? ORDER BY score LIMIT ?",
            (match, paper_id, k)).fetchall()
        return [{"page": r["page"], "text": r["text"], "score": r["score"]} for r in rows]

    # ------------------------------------------------------------ audit trail
    def start_run(self, question, model=None, access_code=None):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (question, model, started_at, access_code) VALUES (?, ?, ?, ?)",
                (question, model, time.time(), access_code))
        return cur.lastrowid

    def runs_by_code(self, access_code):
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE access_code=?", (access_code,)).fetchone()[0]

    def finish_run(self, run_id, report_path):
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at=?, report_path=? WHERE run_id=?",
                              (time.time(), report_path, run_id))

    def log_quote(self, run_id, paper_id, claimed_page, text, verified, found_on=(), reason=None):
        with self.conn:
            self.conn.execute("INSERT INTO quote_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (run_id, paper_id, claimed_page, text, int(verified),
                               json.dumps(list(found_on)), reason, time.time()))

    def run_summary(self, run_id):
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(verified), 0) AS ok FROM quote_checks WHERE run_id=?",
            (run_id,)).fetchone()
        return {"total": row["total"], "verified": row["ok"], "rejected": row["total"] - row["ok"]}
