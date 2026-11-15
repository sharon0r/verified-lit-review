# -*- coding: utf-8 -*-
"""
The research agent: Claude decides what to search, fetch and read; code checks every quote.

    python -m verified_lit_review "Does telephone follow-up after discharge reduce readmissions?" --n 3

Loop (a manual tool-use loop, so every step is visible and logged):
    question -> Claude calls tools -> tool results -> ... -> submit_report
    submit_report -> guardrail verifies every quote against its PDF page
                  -> rejected quotes go back to Claude once, with the page where they really are
                  -> build_report writes the report with verified quotes only

Retrieval is SQL full-text search over the indexed pages (db.py), and every quote check is
written to the audit trail (quote_checks table), together with the run's model and cost.
"""
import json
import os
import time

from .build_report import MIN_QUOTE_CHARS, build_report, norm
from .db import Store
from .fetch import fetch_paper
from .sources import SourceError, search_europepmc

MODEL = "claude-opus-5-5"
PRICE_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                  "claude-haiku-5-5": (0.10, 0.50)}
PROMPT = os.path.join(os.path.dirname(__file__), "prompts", "agent_system.md")
MAX_TURNS = 30
MAX_SUBMITS = 2
PAGE_CHARS = 4000


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


QUOTE = _schema({"page": {"type": "integer"}, "text": {"type": "string"}})
ARTICLE = _schema({
    "paper_id": {"type": "string"},
    "topic": {"type": "string", "description": "the sub-topic this paper answers"},
    "study_type": {"type": "string", "description": "design, sample size, follow-up"},
    "bullets": {"type": "array", "items": {"type": "string"}},
    "relevance": {"type": "string", "description": "how it answers the question + its limits"},
    "quotes": {"type": "array", "items": QUOTE},
})

TOOLS = [
    {"name": "search_papers", "strict": True,
     "description": "Search open-access biomedical papers (Europe PMC). Use English keywords.",
     "input_schema": _schema({"query": {"type": "string"},
                              "n": {"type": "integer", "description": "results, 1-15"}})},
    {"name": "fetch_paper", "strict": True,
     "description": "Download a paper's open-access PDF and index its pages. Call before reading it.",
     "input_schema": _schema({"paper_id": {"type": "string"}})},
    {"name": "retrieve_passages", "strict": True,
     "description": "Return the pages of a fetched paper that best match the query, with page numbers.",
     "input_schema": _schema({"paper_id": {"type": "string"}, "query": {"type": "string"},
                              "k": {"type": "integer", "description": "pages, 1-4"}})},
    {"name": "verify_quote", "strict": True,
     "description": "Check that a quote appears word for word on the given page of a fetched paper.",
     "input_schema": _schema({"paper_id": {"type": "string"}, "page": {"type": "integer"},
                              "text": {"type": "string"}})},
    {"name": "submit_report", "strict": True,
     "description": "Submit the final review. Every quote is verified; failures are returned to you.",
     "input_schema": _schema({"articles": {"type": "array", "items": ARTICLE}})},
]


class Agent:
    def __init__(self, store, pdf_dir, client=None, model=MODEL, effort="medium", on_event=None):
        self.store, self.pdf_dir, self.model, self.effort = store, pdf_dir, model, effort
        self._client = client
        self.on_event = on_event or (lambda kind, msg: None)
        self.fetched, self.submissions, self.final, self.done = set(), 0, None, False
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    @property
    def client(self):
        if self._client is None:
            import anthropic                      # imported lazily: tests run without the SDK/key
            self._client = anthropic.Anthropic()
        return self._client

    # ------------------------------------------------------------------ tools
    def search_papers(self, query, n=8):
        papers, status = search_europepmc(query, self.store, n=max(1, min(int(n), 15)))
        self.on_event("search", f"'{query}': {len(papers)} papers ({status})")
        return [{"paper_id": self.store.upsert_paper(p), "title": p["title"], "year": p["year"],
                 "journal": p["journal"], "citations": p.get("citations", 0),
                 "abstract": (p.get("abstract") or "")[:1200]} for p in papers]

    def fetch_paper(self, paper_id):
        paper = self.store.get_paper(paper_id)
        if not paper:
            return {"error": "unknown paper_id; use one returned by search_papers"}
        r = fetch_paper(paper, self.store, self.pdf_dir)
        self.on_event("fetch", f"{paper_id}: {r['status']}" + (f", {r['pages']} pages" if r.get("pages") else ""))
        if r["status"] == "failed":
            return {"status": "failed", "reason": "no open-access PDF could be downloaded; choose another paper"}
        self.fetched.add(paper_id)
        return {"status": "ok", "pages": r["pages"]}

    def retrieve_passages(self, paper_id, query, k=3):
        if paper_id not in self.fetched:
            return {"error": "fetch_paper first"}
        hits = self.store.search_pages(paper_id, query, k=max(1, min(int(k), 4)))
        self.on_event("retrieve", f"{paper_id}: pages {[h['page'] for h in hits]} for '{query}'")
        return [{"page": h["page"], "text": h["text"][:PAGE_CHARS]} for h in hits]

    def _check(self, paper_id, page, text):
        """The guardrail, on the indexed page text. Returns (verified, found_on_pages, reason)."""
        n = norm(text)
        if len(n) < MIN_QUOTE_CHARS:
            return False, [], "too short to verify"
        rows = self.store.conn.execute("SELECT page, text FROM pages WHERE paper_id=?", (paper_id,)).fetchall()
        found = [r["page"] for r in rows if n in norm(r["text"])]
        if page in found:
            return True, found, None
        return False, found, (f"not on page {page}; found on {found}" if found else "not found in the paper")

    def verify_quote(self, paper_id, page, text):
        ok, found, reason = self._check(paper_id, int(page), text)
        return {"verified": ok, "found_on_pages": found, "reason": reason}

    def submit_report(self, articles):
        self.submissions += 1
        failures = []
        for a in articles:
            for q in a["quotes"]:
                ok, found, reason = self._check(a["paper_id"], int(q["page"]), q["text"])
                if not ok:
                    failures.append({"paper_id": a["paper_id"], "page": q["page"], "text": q["text"][:120],
                                     "reason": reason})
        total = sum(len(a["quotes"]) for a in articles)
        self.on_event("verify", f"submission {self.submissions}: {total - len(failures)}/{total} quotes verified")
        self.final = articles
        if failures and self.submissions < MAX_SUBMITS:
            return {"accepted": False, "failures": failures,
                    "instruction": "Fix or remove these quotes (copy exactly from retrieved pages) and submit again."}
        self.done = True
        return {"accepted": True, "note": "unverified quotes, if any, will be left out of the report"}

    def run_tool(self, name, args):
        try:
            if name not in {t["name"] for t in TOOLS}:
                return {"error": f"unknown tool {name}"}
            return getattr(self, name)(**args)
        except (SourceError, ValueError) as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ loop
    def run(self, question, n_papers=3, language="he", out_path="Research_Summary_Report.md"):
        run_id = self.store.start_run(question, self.model)
        system = open(PROMPT, encoding="utf-8").read()
        lang = {"he": "Hebrew", "en": "English"}.get(language, language)
        messages = [{"role": "user", "content":
                     f"Research question: {question}\n\nReview exactly {n_papers} papers. "
                     f"Write findings, topics and relevance in {lang}."}]
        t0 = time.time()
        for _turn in range(MAX_TURNS):
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system, tools=TOOLS, messages=messages,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            self.usage["input_tokens"] += response.usage.input_tokens
            self.usage["output_tokens"] += response.usage.output_tokens
            if response.stop_reason == "refusal":
                raise RuntimeError("the model declined this request")
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                break
            results = []
            for call in calls:
                out = self.run_tool(call.name, call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(out, ensure_ascii=False),
                                "is_error": isinstance(out, dict) and "error" in out})
            messages.append({"role": "user", "content": results})
            if self.done:
                break
        if not self.final:
            raise RuntimeError("the agent finished without submitting a report")
        return self._write_report(run_id, question, language, out_path, time.time() - t0)

    def _write_report(self, run_id, question, language, out_path, seconds):
        arts = []
        for a in self.final:
            p = self.store.get_paper(a["paper_id"]) or {}
            if not p.get("pdf_path"):
                continue
            arts.append({"file": os.path.abspath(p["pdf_path"]), "title": p["title"], "authors": p.get("authors"),
                         "year": p.get("year"), "journal": p.get("journal"), "doi": p.get("doi"),
                         "topic": a["topic"], "study_type": a["study_type"], "bullets": a["bullets"],
                         "relevance": a["relevance"], "quotes": a["quotes"]})
            for q in a["quotes"]:
                ok, found, reason = self._check(a["paper_id"], int(q["page"]), q["text"])
                self.store.log_quote(run_id, a["paper_id"], int(q["page"]), q["text"], ok, found, reason)
        spec = {"question": question, "language": language, "articles": arts}
        ok_n, rejected = build_report(spec, "", out_path)          # independent re-check on the PDFs
        self.store.finish_run(run_id, out_path)
        cost = self.cost()
        return {"run_id": run_id, "report": out_path, "verified": ok_n, "rejected": rejected,
                "papers": len(arts), "seconds": round(seconds, 1), "usage": dict(self.usage), "cost_usd": cost}

    def cost(self):
        inp, out = PRICE_PER_MTOK.get(self.model, (0, 0))
        return round(self.usage["input_tokens"] / 1e6 * inp + self.usage["output_tokens"] / 1e6 * out, 4)
