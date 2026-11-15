# Verified Literature Review Agent

**An AI agent that answers a research question in any field (medicine, education, social sciences, ...) with a short literature review, where every quote is machine-checked against the exact PDF page it cites.** Agentic RAG with Claude, SQLite full-text retrieval, a code-level anti-hallucination guardrail and an audit trail, exposed as a CLI, a web UI and a REST API.

[![tests](https://github.com/sharon0r/verified-lit-review/actions/workflows/tests.yml/badge.svg)](https://github.com/sharon0r/verified-lit-review/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

> **בקצרה:** בניתי ב-Python סוכן AI לשלב הראשון של מחקר: איתור מאמרים וסקירת ספרות. נותנים לו שאלת מחקר ומספר מאמרים. הסוכן (Claude) מחפש מאמרים בגישה פתוחה מכל תחום, מרפואה ועד חינוך, מוריד אותם, שומר כל עמוד במסד נתונים SQLite, שולף את העמודים הרלוונטיים וכותב סיכום עם ציטוטים (RAG). לפני שציטוט נכנס לדו"ח, הקוד מוודא שהוא מופיע מילה במילה ומספר במספר בעמוד שצוין. ציטוט שגוי חוזר לסוכן לתיקון, וכל בדיקה נשמרת במסד הנתונים לתיעוד. אפשר להריץ את הכלי משורת הפקודה, מממשק בדפדפן או דרך API.

---

## Why

Language models are good at summarizing research and bad at citing it: quotes that sound right but are slightly altered, or a real sentence attached to the wrong page. In a hospital that is a patient-safety problem, not a style problem. Here the model may *propose* quotes, but only code decides what reaches the report.

## How it works

```mermaid
flowchart LR
    Q[Research question] --> A{{Claude agent}}
    A -- search_papers --> S[Europe PMC / OpenAlex<br/>cached in SQLite]
    A -- fetch_paper --> F[Open-access PDF<br/>PMC on AWS / OpenAlex]
    F --> DB[(SQLite<br/>FTS5 page index)]
    A -- retrieve_passages --> DB
    A -- submit_report --> G{Guardrail:<br/>quote on that page?}
    G -- no: page where it really is --> A
    G -- yes --> R[Report .md / .docx]
    G --> AU[(Audit trail<br/>every quote check)]
```

1. **Agent.** Claude splits the question into sub-topics, picks the database (Europe PMC for medicine and life sciences, OpenAlex for every other field), writes English search queries (also for Hebrew questions), reads abstracts and picks papers. If a PDF cannot be downloaded it chooses another paper.
2. **Retrieval.** Each PDF page is stored in a SQLite FTS5 table; the agent reads only the pages that BM25 ranks as relevant.
3. **Generation.** Findings, caveats and 1-3 exact quotes per paper, with page numbers.
4. **Guardrail.** Every quote is normalized and must appear on the cited page. Failures go back to the agent once, with the page where the sentence really is; the final report is re-checked against the PDFs and keeps verified quotes only.
5. **Audit trail.** Every quote check, the model, the PDF's SHA-256 and the run cost are stored in SQLite.

### The guardrail

| Ignored (layout noise) | Kept (meaning) |
|---|---|
| case, spaces, line breaks, hyphenation, punctuation, ligatures (`ﬁ` → `fi`), dash variants | every word in any script, every digit, the decimal point (`1.1` ≠ `11`), minus signs, `<` `>` `≤` `≥` `%`, the page number |

`p>0.05` instead of `p<0.05`, one changed number, or a correct sentence on the wrong page are all rejected. Quotes shorter than 20 normalized characters are rejected as too unspecific to prove anything.

## Example run

Question: *Does telephone follow-up after hospital discharge reduce readmissions?* ([full log](examples/telephone-follow-up/run_log.txt) · [report, Hebrew](examples/telephone-follow-up/Research_Summary_Report.md))

```text
🔍 'telephone follow-up after hospital discharge readmission randomized trial': 10 papers (network)
🔍 'post-discharge telephone calls readmission systematic review meta-analysis': 0 papers (network)
🔍 'telephone follow-up discharge readmission meta-analysis': 2 papers (network)
⬇  10.2196/80529: cached, 14 pages
⬇  10.1093/ageing/afaa002: ok, 12 pages
⬇  10.1007/s12603-016-0745-7: ok, 8 pages
📄 10.2196/80529: pages [7, 13, 6] for 'results readmission ED visits 30 days intervention vs control odds ratio'
✅ submission 1: 6/6 quotes verified
💲 52,344 input + 3,869 output tokens ≈ $0.29 · 55 s
```

The agent reported honestly that the largest trial (n = 7,091) found **no** significant reduction in readmissions, only in emergency-department visits, and balanced it with a meta-analysis and a randomized trial.

A second run, on an education question (*Does formative assessment with feedback improve student achievement?*): the agent chose OpenAlex on its own, fetched three papers (two meta-analyses and a controlled study) and verified 6/6 quotes for $0.31 ([report](examples/formative-assessment/Research_Summary_Report.md)).

## Run it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...          # never commit it; .env and *.db are git-ignored

python -m verified_lit_review "Does telephone follow-up after discharge reduce readmissions?" --n 3   # CLI
streamlit run app.py                                                                                 # web UI
uvicorn verified_lit_review.api:app --app-dir src                                                    # REST API, docs at /docs
```

(On Windows PowerShell use `$env:ANTHROPIC_API_KEY = "..."`; for the CLI and API set `PYTHONPATH=src` or `pip install -e .`.)

**Cloud (Streamlit Community Cloud).** Deploy `app.py` from this repo and paste the keys from [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example) into the app's *Secrets*. The key never enters the repository. Because every run is paid for with the owner's API key, the hosted app is protected by `APP_PASSWORD`, a `DAILY_RUN_LIMIT` (default 10 per 24 h) and `MAX_PAPERS` (default 3), on top of a monthly spend limit set in the Anthropic console.

REST API:

| Method | Path | |
|---|---|---|
| `POST` | `/reviews` | `{"question": "...", "n_papers": 3, "language": "he"}` → report Markdown, verified and rejected quotes, cost |
| `GET` | `/runs` | recent runs with verification counts |
| `GET` | `/runs/{id}` | one run with its full audit trail |

## Design decisions

- **Two open databases, chosen per question.** Europe PMC is precise for biomedicine; OpenAlex covers every discipline. The agent decides, and falls back to the other when results are thin.
- **Official APIs only, politely.** No scraping. An honest User-Agent, at most one request per second per host, exponential back-off that honours `Retry-After`, and a SQLite cache, so a repeated question makes no network calls and still works offline.
- **A manual tool-use loop** instead of a framework, so every step is visible, testable and logged.
- **The guardrail lives in code, not in the prompt.** The prompt asks for exact quotes; the code checks them.
- **Tests never call the model.** A scripted fake model drives the agent loop, so CI is free and deterministic.

## Project structure

```text
src/verified_lit_review/
  agent.py              Claude tool-use loop, guardrail feedback, cost tracking
  prompts/agent_system.md  the execution prompt (kept separate from code)
  db.py                 SQLite: search cache, papers, FTS5 page index, runs, quote_checks
  sources.py            polite Europe PMC + OpenAlex clients (rate limit, back-off, offline cache)
  fetch.py              open-access PDF download (PMC on AWS, OpenAlex) + page indexing
  build_report.py       quote normalization + verified Markdown report
  service.py            one entry point for CLI, UI and API
  api.py                FastAPI REST API
  md_to_docx.py         Markdown → Word, right-to-left Hebrew aware
app.py                  Streamlit web UI (Hebrew, right-to-left)
tests/                  45 tests: guardrail, store, sources, fetch, agent loop, API
```

## Tests

```bash
python -m pytest
```

No API key or network is needed: PDFs are generated on the fly, HTTP is faked, and the agent runs against a scripted model. The agent tests cover a misattributed quote being sent back and fixed, an altered number being dropped, and reading before fetching being refused.

## Lessons learned

- **Normalization is a safety decision.** A first version stripped everything except Latin letters and digits, so `p<0.05` matched `p>0.05` and Hebrew quotes could never verify. A code review caught it.
- **Read the API's real behaviour.** Europe PMC answers `sort=RELEVANCE` with HTTP 503, which looked like an outage; the default order is already relevance.
- **Verification must be per page**, not per document, or a correct sentence with a wrong page number passes.
- **Publisher sites block scripts even for open papers.** The PMC open-data bucket on AWS is the reliable route.

## Limitations

- Only quotes are verified mechanically. Findings and paper choice still need expert review; the report states each paper's limits.
- Open-access papers only; scanned PDFs without a text layer are skipped (no OCR).
- In a hospital deployment the model would run under the organization's data agreements, and no patient data should ever be sent. This tool only handles published papers.

## How it was built

Designed and built with [Claude Code](https://claude.com/claude-code) as an AI pair programmer. The requirements, the guardrail design, the choice of sources and the review of every output were mine.

## License

© 2026 sharon0r. **All rights reserved.** Shared for viewing and evaluation only; using, copying or modifying the code requires written permission. See [LICENSE](LICENSE). The example report quotes short passages from open-access articles for scholarly purposes; each article remains under its original license.
