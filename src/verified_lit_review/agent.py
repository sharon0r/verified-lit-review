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
import re
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
MAX_SUBMITS = 2            # one send-back; fix_report makes it cheap (only the flagged items)
# Opus 5.5 always thinks, and thinking counts against max_tokens. A cloud run hit a 16000 cap twice
# while rewriting a 5-paper report (110 s each, nothing usable), so the limit is higher and streamed.
MAX_OUTPUT_TOKENS = 48000
CONCLUSION_MAX_WORDS = 130  # about 5 sentences; a physician reviewer found a 12-sentence, 25-number one
BOTTOM_LINE_MAX_WORDS = 55
PAGE_CHARS = 4000
# Every turn re-sends the whole conversation, so turns cost both time and money. Measured runs spent
# up to 10 turns searching again after failed downloads; the code now ends the search phase.
MAX_SEARCH_TURNS = 3
ABSTRACT_CHARS = 800
# "Woodend and colleagues", "Smith et al.", "Smith ועמיתיה", "(Smith, 2016)": a name used to cite a paper.
# A hospital reviewer found an invented author name in a conclusion; numbers were checked, names were not.
CITED_NAME = re.compile(r"\b([A-Z][A-Za-z'\-]{2,})(?=\s*(?:et al|and colleagues|and co-workers|ועמית|ושות|וחבר"
                        r"|,\s*(?:19|20)\d\d))")


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
NOT_READ = _schema({
    "paper_id": {"type": "string"},
    "why_important": {"type": "string",
                      "description": "one short sentence: why a reader should open this paper (its design and "
                                     "scope), with no results, numbers or direction of effect"},
})
MAX_NOT_READ = 3
REPORT_FIELDS = ["bottom_line", "conclusion", "certainty", "pico"]
ARTICLE_FIELDS = ["topic", "study_type", "why_chosen", "relevance"]
EDIT = _schema({
    "field": {"type": "string", "enum": REPORT_FIELDS + ARTICLE_FIELDS + ["bullet", "quote"]},
    "paper_id": {"type": "string", "description": "the paper for article fields, bullet and quote; else empty"},
    "index": {"type": "integer", "description": "bullet or quote position from 0; -1 adds a new one"},
    "page": {"type": "integer", "description": "page of a quote; 0 otherwise"},
    "text": {"type": "string", "description": "the new text; empty removes the bullet or quote"},
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
     "description": "Download a paper's open-access PDF and index its pages. Call before reading it. "
                    "About a third of downloads fail (publishers block them): in the same turn, fetch "
                    "your chosen papers plus 1-2 spare candidates, then use the ones that worked.",
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
         "bottom_line": {"type": "string",
                         "description": "at most 2 short sentences shown first: the direction of the evidence, "
                                        "its certainty, and the main caveat (e.g. papers study a different "
                                        "intervention than the question)"},
         "certainty": {"type": "string",
                       "description": "GRADE-style certainty for the main outcome (high/moderate/low/very low) "
                                      "with one short reason per downgrade"},
         "conclusion": {"type": "string",
                        "description": f"at most 5 sentences and {CONCLUSION_MAX_WORDS} words answering the "
                                       "question across all papers: direction, strength and consistency, main "
                                       "gaps; at most 4 key estimates (other numbers stay in the findings)"},
         "articles": {"type": "array", "items": ARTICLE},
         "not_read": {"type": "array", "items": NOT_READ,
                      "description": "up to 3 important papers from the search results that could not be "
                                     "downloaded; empty if none"}})},
    {"name": "fix_report", "strict": True,
     "description": "After a send-back: change only the flagged items of your last submission. The whole "
                    "report is checked again, exactly as submit_report does. Much faster than resubmitting.",
     "input_schema": _schema({"edits": {"type": "array", "items": EDIT}})},
]


class Agent:
    def __init__(self, store, pdf_dir, client=None, model=MODEL, effort="medium", on_event=None,
                 should_stop=None):
        self.store, self.pdf_dir, self.model, self.effort = store, pdf_dir, model, effort
        self._client = client
        self.on_event = on_event or (lambda kind, msg: None)
        self.should_stop = should_stop or (lambda: False)   # checked before every paid model call
        self.fetched, self.submissions, self.final, self.done = set(), 0, None, False
        self.search_turns = 0
        self.author_words = set()  # every word of every author list the searches returned
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
        if self.search_turns > MAX_SEARCH_TURNS:
            return {"error": f"search budget used ({MAX_SEARCH_TURNS} search turns). Choose from the "
                             "candidates you already have; review fewer papers if no direct one is left."}
        search = SEARCHERS.get(database, SEARCHERS["openalex"])
        papers, status = search(query, self.store, n=max(1, min(int(n), 15)))
        self.on_event("search", f"[{database}] '{query}': {len(papers)} papers ({status})")
        for p in papers:
            self.author_words.update(w.lower() for w in re.findall(r"[^\W\d_]{2,}", p.get("authors") or ""))
        # authors are given so the agent never has to name a paper's authors from memory
        return [{"paper_id": self.store.upsert_paper(p), "title": p["title"], "year": p["year"],
                 "authors": (p.get("authors") or "")[:80],
                 "journal": p["journal"], "citations": p.get("citations", 0),
                 "abstract": (p.get("abstract") or "")[:ABSTRACT_CHARS]} for p in papers]

    def fetch_paper(self, paper_id):
        paper = self.store.get_paper(paper_id)
        if not paper:
            return {"error": "unknown paper_id; use one returned by search_papers"}
        r = fetch_paper(paper, self.store, self.pdf_dir)
        self.on_event("fetch", f"{paper_id}: {r['status']}" + (f", {r['pages']} pages" if r.get("pages") else ""))
        if r["status"] == "failed":
            return {"status": "failed", "reason": "no open-access PDF could be downloaded; use a spare or "
                                                  "another candidate you already found"}
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

    def _name_check(self, articles, conclusion):
        """Author names used to cite a paper that appear in no search result and in no fetched paper."""
        texts = [conclusion] + [" ".join([a.get("why_chosen", ""), a.get("relevance", "")] + a.get("bullets", []))
                                for a in articles]
        for a in articles:
            self.author_words.update(w.lower() for w in re.findall(
                r"[^\W\d_]{2,}", (self.store.get_paper(a["paper_id"]) or {}).get("authors") or ""))
        bad = []
        for name in {m for t in texts for m in CITED_NAME.findall(t or "")}:
            if name.lower() in self.author_words:
                continue
            found = self.store.conn.execute(
                "SELECT 1 FROM pages WHERE paper_id IN ({}) AND text LIKE ? LIMIT 1".format(
                    ",".join("?" * len(self.fetched)) or "''"), (*self.fetched, f"%{name}%")).fetchone()
            if not found:
                bad.append(name)
        return sorted(bad)

    def verify_quote(self, paper_id, page, text):
        ok, found, reason = self._check(paper_id, int(page), text)
        return {"verified": ok, "found_on_pages": found, "reason": reason}

    def _not_read_check(self, not_read, articles):
        """Keep listed unread papers that came from the searches; refuse results in their sentence."""
        chosen = {a["paper_id"] for a in articles}
        kept, problems = [], []
        for item in (not_read or [])[:MAX_NOT_READ]:
            pid, why = item.get("paper_id", ""), " ".join((item.get("why_important") or "").split())
            if pid in chosen or not self.store.get_paper(pid):
                continue                     # unknown id or a paper that is in the report anyway
            if re.search(r"\d", re.sub(r"\b(?:19|20)\d\d\b", "", why)):
                problems.append(pid)         # a number = a result taken from an unread abstract
            kept.append({"paper_id": pid, "why_important": why})
        return kept, problems

    def fix_report(self, edits):
        """Apply small edits to the last submission and check the whole report again."""
        last = getattr(self, "last_submission", None)
        if not last:
            return {"error": "nothing to fix yet: call submit_report first"}
        rep = json.loads(json.dumps(last))                        # deep copy
        by_id = {a["paper_id"]: a for a in rep["articles"]}
        for e in edits:
            f, text = e["field"], e["text"]
            if f in REPORT_FIELDS:
                rep[f] = text
                continue
            a = by_id.get(e["paper_id"])
            if a is None:
                return {"error": f"unknown paper_id {e['paper_id']!r}; use one from your submission"}
            if f in ARTICLE_FIELDS:
                a[f] = text
                continue
            items, i = (a["bullets"], e["index"]) if f == "bullet" else (a["quotes"], e["index"])
            new = text if f == "bullet" else {"page": e["page"], "text": text}
            if i == -1 and text:
                items.append(new)
            elif 0 <= i < len(items):
                if text:
                    items[i] = new
                else:
                    items.pop(i)
            else:
                return {"error": f"{f} index {i} is out of range for {e['paper_id']}"}
        return self.submit_report(**rep)

    def _length_check(self, conclusion, bottom_line):
        long = []
        if len(conclusion.split()) > CONCLUSION_MAX_WORDS:
            long.append(f"conclusion has {len(conclusion.split())} words; at most {CONCLUSION_MAX_WORDS}, "
                        "with at most 4 key estimates")
        if len(bottom_line.split()) > BOTTOM_LINE_MAX_WORDS:
            long.append(f"bottom_line must be 1-2 sentences, at most {BOTTOM_LINE_MAX_WORDS} words")
        return long

    def submit_report(self, articles, conclusion="", pico="", certainty="", not_read=None, bottom_line=""):
        self.last_submission = {"articles": articles, "conclusion": conclusion, "pico": pico,
                                "certainty": certainty, "not_read": not_read or [], "bottom_line": bottom_line}
        self.conclusion, self.pico, self.certainty, self.bottom_line = conclusion, pico, certainty, bottom_line
        self.not_read, not_read_numbers = self._not_read_check(not_read, articles)
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
        bad_names = self._name_check(articles, conclusion + " " + bottom_line)
        too_long = self._length_check(conclusion, bottom_line)
        total = sum(len(a["quotes"]) for a in articles)
        n_bad = sum(len(w["numbers_not_in_paper"]) for w in number_warnings)
        retry = ((failures or number_warnings or bad_names or not_read_numbers or too_long)
                 and self.submissions < MAX_SUBMITS)
        self.on_event("verify", f"submission {self.submissions}: {total - len(failures)}/{total} quotes verified"
                      + (f", {n_bad} number(s) not found in the papers" if n_bad else "")
                      + (f", author name(s) not in any paper: {', '.join(bad_names)}" if bad_names else "")
                      + (", text too long" if too_long else "")
                      + (" - sent back to the agent for a quick fix" if retry else ""))
        self.final = articles
        if retry:
            return {"accepted": False, "failures": failures, "number_warnings": number_warnings,
                    "author_names_not_found": bad_names, "not_read_with_numbers": not_read_numbers,
                    "too_long": too_long,
                    "instruction": "Call fix_report with edits for only these items; do not resubmit the whole "
                                   "report. Quotes: copy exactly from retrieved pages, or remove them. Numbers "
                                   "not found in the paper: use the exact value the paper prints, or remove it; "
                                   "never compute new numbers. A number in the conclusion must also appear in "
                                   "a finding or quote of the paper it comes from. Author names not found: use "
                                   "the first author exactly as the search results list it. not_read papers: "
                                   "say only why they matter, with no numbers or results. Too long: shorten."}
        self.done = True
        return {"accepted": True, "note": "unverified quotes, if any, will be left out of the report"}

    def run_tool(self, name, args):
        try:
            if name not in {t["name"] for t in TOOLS}:
                return {"error": f"unknown tool {name}"}
            return getattr(self, name)(**args)
        except (SourceError, ValueError) as e:
            return {"error": str(e)}
        except (TypeError, KeyError) as e:   # e.g. arguments cut off at the output limit
            return {"error": f"missing or incomplete arguments for {name} ({e}); send the call again"}
        except Exception as e:  # noqa: BLE001 - one failing tool call must not end the whole run
            return {"error": f"{type(e).__name__} in {name}; try another approach"}

    # ------------------------------------------------------------------ loop
    def _create(self, **kw):
        """One model call. Streamed, since a large max_tokens needs streaming in the SDK."""
        api = self.client.beta.messages
        if hasattr(api, "stream"):
            with api.stream(**kw) as s:
                return s.get_final_message()
        return api.create(**kw)                    # test doubles

    def run(self, question, n_papers=3, language="he", out_path="Research_Summary_Report.md"):
        run_id = self.store.start_run(question, self.model)
        system = open(PROMPT, encoding="utf-8").read()
        lang = {"he": "Hebrew", "en": "English"}.get(language, language)
        messages = [{"role": "user", "content":
                     f"Research question: {question}\n\nReview {n_papers} papers (fewer only if "
                     f"there are not enough direct, fetchable ones). "
                     f"Write findings, topics and relevance in {lang}."}]
        t0 = time.time()
        self.timing = {"turns": 0, "model_seconds": 0.0, "tool_seconds": 0.0}
        try:
            for _turn in range(MAX_TURNS):
                if self.should_stop():
                    raise Cancelled("stopped by the user")
                t_model = time.time()
                response = self._create(
                    model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=system, tools=TOOLS,
                    messages=messages, output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},   # each turn re-sends the history: cache the prefix
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                model_s = time.time() - t_model
                self.timing["turns"] += 1
                self.timing["model_seconds"] += model_s
                for k in self.usage:
                    self.usage[k] += getattr(response.usage, k, 0) or 0
                self.served_models.add(getattr(response, "model", None) or self.model)
                if response.stop_reason == "refusal":
                    raise RuntimeError("the model declined this request")
                if self.cost() > MAX_COST_USD:
                    raise RuntimeError(f"cost limit reached (${self.cost():.2f} > ${MAX_COST_USD:.2f} per run)")
                messages.append({"role": "assistant", "content": response.content})
                calls = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason == "max_tokens":
                    # the last tool call is cut off: never run it, say why, and ask for a smaller step
                    note = ("Your reply was cut off at the output limit, so this call was not run. Send it "
                            "again, shorter; after a send-back use fix_report with only the flagged items.")
                    self.on_event("timing", f"turn {self.timing['turns']}: model {model_s:.0f}s - cut off at "
                                  "the output limit, asked for a shorter reply")
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": c.id, "content": note, "is_error": True}
                        for c in calls] or [{"type": "text", "text": note}]})
                    continue
                if not calls:
                    break
                if any(c.name == "search_papers" for c in calls):
                    self.search_turns += 1
                results, t_tools = [], time.time()
                for call in calls:
                    out = self.run_tool(call.name, call.input)
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": json.dumps(out, ensure_ascii=False),
                                    "is_error": isinstance(out, dict) and "error" in out})
                tools_s = time.time() - t_tools
                self.timing["tool_seconds"] += tools_s
                self.on_event("timing", f"turn {self.timing['turns']}: model {model_s:.0f}s "
                              f"({getattr(response.usage, 'output_tokens', 0)} tokens), "
                              f"{len(calls)} tool call(s) {tools_s:.0f}s")
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
                "certainty": getattr(self, "certainty", ""),
                "bottom_line": getattr(self, "bottom_line", ""),
                "not_read": [{"title": p["title"], "authors": p.get("authors"), "year": p.get("year"),
                              "journal": p.get("journal"), "doi": p.get("doi"), "pmcid": p.get("pmcid"),
                              "url": p.get("url"), "why_important": n["why_important"]}
                             for n in getattr(self, "not_read", []) for p in [self.store.get_paper(n["paper_id"])]
                             if p]}
        ok_n, rejected = build_report(spec, "", out_path)          # independent re-check on the PDFs
        cost = self.cost()
        self.store.finish_run(run_id, out_path, cost_usd=cost, served_model=self._served())
        return {"run_id": run_id, "report": out_path, "verified": ok_n, "rejected": rejected,
                "papers": len(arts), "seconds": round(seconds, 1), "timing": dict(getattr(self, "timing", {})), "usage": dict(self.usage), "cost_usd": cost,
                "served_model": self._served()}

    def _served(self):
        return ",".join(sorted(m for m in self.served_models if m)) or None

    def cost(self):
        """Prompt caching: cache writes cost 1.25x the input price, cache reads 0.1x."""
        inp, out = PRICE_PER_MTOK.get(self.model, (0, 0))
        u = self.usage
        tokens_in = u["input_tokens"] + 1.25 * u["cache_creation_input_tokens"] + 0.1 * u["cache_read_input_tokens"]
        return round(tokens_in / 1e6 * inp + u["output_tokens"] / 1e6 * out, 4)
