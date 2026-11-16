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

from . import numbers
from .build_report import MIN_QUOTE_CHARS, build_report, norm
from .db import Store
from .fetch import fetch_paper
from .sources import SEARCHERS, SourceError

MODEL = "claude-opus-5-5"
PRICE_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                  "claude-haiku-5-5": (0.10, 0.50)}
PROMPT = os.path.join(os.path.dirname(__file__), "prompts", "agent_system.md")
DATABASE_NAMES = {"europepmc": "Europe PMC", "openalex": "OpenAlex", "pubmed": "PubMed"}
MAX_TURNS = 30


class Cancelled(RuntimeError):
    """The user stopped the run (checked before each model call, so no further cost)."""
MAX_COST_USD = float(os.environ.get("MAX_COST_PER_RUN", "1.00"))   # hard stop for one runaway run
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
    "why_chosen": {"type": "string",
                   "description": "one sentence: why this paper was selected over the other candidates "
                                  "(design strength, directness, recency, population)"},
    "bullets": {"type": "array", "items": {"type": "string"}},
    "relevance": {"type": "string", "description": "how it answers the question + its limits"},
    "quotes": {"type": "array", "items": QUOTE},
})

TOOLS = [
    {"name": "search_papers", "strict": True,
     "description": "Search open-access papers. database 'pubmed_reviews' = only systematic reviews and "
                    "meta-analyses from the last 10 years in health sciences (PubMed); 'europepmc' = medicine "
                    "and life sciences, all study types; 'openalex' = every discipline (education, social "
                    "sciences, engineering, ...). Use English keywords.",
     "input_schema": _schema({"query": {"type": "string"},
                              "database": {"type": "string", "enum": ["pubmed_reviews", "europepmc", "openalex"]},
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
     "input_schema": _schema({
         "pico": {"type": "string",
                  "description": "the question as PICO: Population; Intervention/exposure; Comparison; Outcome"},
         "certainty": {"type": "string",
                       "description": "GRADE-style certainty for the main outcome (high/moderate/low/very low) "
                                      "with one short reason per downgrade"},
         "conclusion": {"type": "string",
                        "description": "3-5 sentences answering the research question across all papers: "
                                       "the overall direction, how strong and consistent the evidence is, "
                                       "and the main gaps"},
         "articles": {"type": "array", "items": ARTICLE}})},
]


class Agent:
    def __init__(self, store, pdf_dir, client=None, model=MODEL, effort="medium", on_event=None,
                 should_stop=None):
        self.store, self.pdf_dir, self.model, self.effort = store, pdf_dir, model, effort
        self._client = client
        self.on_event = on_event or (lambda kind, msg: None)
        self.should_stop = should_stop or (lambda: False)   # checked before every paid model call
        self.fetched, self.submissions, self.final, self.done = set(), 0, None, False
        self.usage = {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.served_models = set()
        self.bounced = []          # quotes the guardrail rejected during the run (audit trail)
        self._paper_numbers = {}   # paper_id -> numbers.PaperNumbers (cached per run)

    @property
    def client(self):
        if self._client is None:
            import anthropic                      # imported lazily: tests run without the SDK/key
            self._client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        return self._client

    # ------------------------------------------------------------------ tools
    def search_papers(self, query, database="openalex", n=8):
        search = SEARCHERS.get(database, SEARCHERS["openalex"])
        papers, status = search(query, self.store, n=max(1, min(int(n), 15)))
        self.on_event("search", f"[{database}] '{query}': {len(papers)} papers ({status})")
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
        # labelled as document data: text inside a PDF is never an instruction to the agent
        return [{"page": h["page"], "untrusted_document_text": h["text"][:PAGE_CHARS]} for h in hits]

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

    def _numbers(self, paper_id):
        if paper_id not in self._paper_numbers:
            rows = self.store.conn.execute("SELECT text FROM pages WHERE paper_id=?", (paper_id,)).fetchall()
            self._paper_numbers[paper_id] = numbers.PaperNumbers([r["text"] for r in rows])
        return self._paper_numbers[paper_id]

    def _number_check(self, article):
        """Numbers in the findings and study description that the paper does not contain."""
        text = " ".join(article.get("bullets", [])) + " " + article.get("study_type", "")
        return numbers.check(text, self._numbers(article["paper_id"]))

    def _conclusion_check(self, articles, conclusion):
        """Numbers in the conclusion must come from the verified evidence of a paper: a finding whose
        number was found in that paper, or a verified quote. Checking against the pooled text of all
        papers let a number pass because it happened to appear somewhere else in some paper."""
        traced = []
        for a in articles:
            traced += self._number_check(a)[0]
            traced += [q["text"] for q in a["quotes"] if self._check(a["paper_id"], int(q["page"]), q["text"])[0]]
        return numbers.check(conclusion, numbers.PaperNumbers([" ".join(traced)]))

    def verify_quote(self, paper_id, page, text):
        ok, found, reason = self._check(paper_id, int(page), text)
        return {"verified": ok, "found_on_pages": found, "reason": reason}

    def submit_report(self, articles, conclusion="", pico="", certainty=""):
        self.conclusion, self.pico, self.certainty = conclusion, pico, certainty
        if not articles:
            return {"error": "submit at least one paper; if none can answer the question, say so in a "
                             "paper's relevance field instead of submitting nothing"}
        unread = [a["paper_id"] for a in articles if a["paper_id"] not in self.fetched]
        if unread:
            return {"error": f"these papers were never fetched and read: {unread}"}
        self.submissions += 1
        failures = []
        for a in articles:
            for q in a["quotes"]:
                ok, found, reason = self._check(a["paper_id"], int(q["page"]), q["text"])
                if not ok:
                    failures.append({"paper_id": a["paper_id"], "page": q["page"], "text": q["text"][:120],
                                     "reason": reason})
                    self.bounced.append({"submission": self.submissions, "paper_id": a["paper_id"],
                                         "page": int(q["page"]), "text": q["text"], "found_on": found,
                                         "reason": reason})
        number_warnings = [{"paper_id": a["paper_id"], "numbers_not_in_paper": bad}
                           for a in articles for bad in [self._number_check(a)[1]] if bad]
        _, bad_conclusion = self._conclusion_check(articles, conclusion)
        if bad_conclusion:
            number_warnings.append({"paper_id": "conclusion", "numbers_not_in_paper": bad_conclusion})
        total = sum(len(a["quotes"]) for a in articles)
        n_bad = sum(len(w["numbers_not_in_paper"]) for w in number_warnings)
        self.on_event("verify", f"submission {self.submissions}: {total - len(failures)}/{total} quotes verified"
                      + (f", {n_bad} number(s) not found in the papers" if n_bad else ""))
        self.final = articles
        if (failures or number_warnings) and self.submissions < MAX_SUBMITS:
            return {"accepted": False, "failures": failures, "number_warnings": number_warnings,
                    "instruction": "Fix or remove these quotes (copy exactly from retrieved pages). For numbers "
                                   "not found in the paper: use the exact value the paper prints, or remove it; "
                                   "never compute new numbers. A number in the conclusion must also appear in "
                                   "a finding or quote of the paper it comes from. Then submit again."}
        self.done = True
        return {"accepted": True, "note": "unverified quotes, if any, will be left out of the report"}

    def run_tool(self, name, args):
        try:
            if name not in {t["name"] for t in TOOLS}:
                return {"error": f"unknown tool {name}"}
            return getattr(self, name)(**args)
        except (SourceError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - one failing tool call must not end the whole run
            return {"error": f"{type(e).__name__} in {name}; try another approach"}

    # ------------------------------------------------------------------ loop
    def run(self, question, n_papers=3, language="he", out_path="Research_Summary_Report.md"):
        run_id = self.store.start_run(question, self.model)
        system = open(PROMPT, encoding="utf-8").read()
        lang = {"he": "Hebrew", "en": "English"}.get(language, language)
        messages = [{"role": "user", "content":
                     f"Research question: {question}\n\nReview exactly {n_papers} papers. "
                     f"Write findings, topics and relevance in {lang}."}]
        t0 = time.time()
        try:
            for _turn in range(MAX_TURNS):
                if self.should_stop():
                    raise Cancelled("stopped by the user")
                response = self.client.beta.messages.create(
                    model=self.model, max_tokens=16000, system=system, tools=TOOLS, messages=messages,
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},   # each turn re-sends the history: cache the prefix
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                for k in self.usage:
                    self.usage[k] += getattr(response.usage, k, 0) or 0
                self.served_models.add(getattr(response, "model", None) or self.model)
                if response.stop_reason == "refusal":
                    raise RuntimeError("the model declined this request")
                if self.cost() > MAX_COST_USD:
                    raise RuntimeError(f"cost limit reached (${self.cost():.2f} > ${MAX_COST_USD:.2f} per run)")
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
        except Exception as e:
            self.store.finish_run(run_id, cost_usd=self.cost(), served_model=self._served(), error=str(e)[:300])
            raise

    def _write_report(self, run_id, question, language, out_path, seconds):
        arts = []
        for a in self.final:
            p = self.store.get_paper(a["paper_id"]) or {}
            if not p.get("pdf_path"):
                continue
            arts.append({"file": os.path.abspath(p["pdf_path"]), "title": p["title"], "authors": p.get("authors"),
                         "year": p.get("year"), "journal": p.get("journal"), "doi": p.get("doi"),
                         "pmcid": p.get("pmcid"), "topic": a["topic"], "study_type": a["study_type"], "bullets": a["bullets"],
                         "why_chosen": a.get("why_chosen", ""),
                         "numbers": dict(zip(("verified", "unverified"), self._number_check(a))),
                         "database": DATABASE_NAMES.get(p.get("source"), p.get("source")),
                         "download_route": p.get("pdf_route"),
                         "relevance": a["relevance"], "quotes": a["quotes"]})
            for q in a["quotes"]:
                ok, found, reason = self._check(a["paper_id"], int(q["page"]), q["text"])
                self.store.log_quote(run_id, a["paper_id"], int(q["page"]), q["text"], ok, found, reason)
        if not arts:
            raise RuntimeError("none of the submitted papers could be downloaded and read")
        final_quotes = {(a["paper_id"], int(q["page"]), q["text"]) for a in self.final for q in a["quotes"]}
        for b in self.bounced:     # rejected in an earlier submission and then fixed or dropped by the agent
            if (b["paper_id"], b["page"], b["text"]) not in final_quotes:
                self.store.log_quote(run_id, b["paper_id"], b["page"], b["text"], False, b["found_on"],
                                     f"returned to the agent at submission {b['submission']}: {b['reason']}")
        spec = {"question": question, "language": language, "articles": arts,
                "conclusion": getattr(self, "conclusion", ""), "pico": getattr(self, "pico", ""),
                "conclusion_numbers": dict(zip(("verified", "unverified"),
                                               self._conclusion_check(self.final, getattr(self, "conclusion", "")))),
                "certainty": getattr(self, "certainty", "")}
        ok_n, rejected = build_report(spec, "", out_path)          # independent re-check on the PDFs
        cost = self.cost()
        self.store.finish_run(run_id, out_path, cost_usd=cost, served_model=self._served())
        return {"run_id": run_id, "report": out_path, "verified": ok_n, "rejected": rejected,
                "papers": len(arts), "seconds": round(seconds, 1), "usage": dict(self.usage), "cost_usd": cost,
                "served_model": self._served()}

    def _served(self):
        return ",".join(sorted(m for m in self.served_models if m)) or None

    def cost(self):
        """Prompt caching: cache writes cost 1.25x the input price, cache reads 0.1x."""
        inp, out = PRICE_PER_MTOK.get(self.model, (0, 0))
        u = self.usage
        tokens_in = u["input_tokens"] + 1.25 * u["cache_creation_input_tokens"] + 0.1 * u["cache_read_input_tokens"]
        return round(tokens_in / 1e6 * inp + u["output_tokens"] / 1e6 * out, 4)
