# -*- coding: utf-8 -*-
"""
Build a literature-review report from a JSON spec, verifying every quote against the PDF.

    python -I build_report.py <spec.json> [--out Research_Summary_Report.md]

Anti-hallucination guardrail
----------------------------
Each quote is searched for on the exact PDF page it is attributed to (PyMuPDF text).
Matching ignores only case, whitespace, line breaks, hyphenation, punctuation, symbols
and ligatures (e.g. 'ﬁ' -> 'fi'): every word and every number must be on that page.
  * verified quotes  -> go into the report
  * failed quotes    -> are LEFT OUT of the report, logged to <out>.rejected.json and
                        printed (with the page they actually appear on, if any)
Exit code: 0 = all quotes verified, 2 = some rejected (fix and re-run), 1 = spec/file error.

Spec format (JSON, UTF-8)
-------------------------
{
  "question": "research question (shown at the top)",
  "language": "he" | "en",                       # report labels; default "he"
  "pdf_dir": "Research_PDFs",                    # relative to the spec file, or absolute
  "articles": [
    {
      "topic": "sub-topic this article answers",
      "file": "1_Short_title.pdf",
      "title": "...", "authors": "...", "year": "2016",
      "journal": "PLoS ONE 11(12): e0167123. doi:...",
      "study_type": "quasi-experimental, n=268, 12-month follow-up",
      "doi": "10.1371/journal.pone.0167123",        # optional: rendered as a link
      "page_offset": 0,          # printed journal page = PDF page + offset (0 = same / unknown)
      "bullets": ["finding answering the sub-topic", "..."],
      "relevance": "fit to the question + caveats",
      "quotes": [ {"page": 2, "text": "exact sentence copied from the PDF"} ]
    }
  ]
}
"""
import argparse
import json
import os
import re
import sys
import unicodedata

import pymupdf

LABELS = {
    "he": dict(title="דו\"ח סיכום ספרות", count="{} מאמרים", question="שאלת המחקר", table="טבלת סיכום המאמרים",
               cols=["#", "נושא", "שם המאמר", "מחברים", "שנה", "מקור"], article="שם המאמר",
               authors="מחברים", source="מקור", type="סוג המחקר", file="קובץ",
               findings="עיקרי הממצאים", relevance="רלוונטיות והסתייגויות", quotes="ציטוטים מתוך המאמר",
               pdf_page="עמוד PDF", journal_page="עמוד {} בכתב העת", ok="✅ אומת",
               note=("> **אימות ציטוטים:** כל ציטוט נבדק אוטומטית מול טקסט עמוד ה-PDF שאליו הוא משויך. "
                     "ציטוטים שלא אומתו הוסרו מהדו\"ח."),
               summary="**סיכום אימות:** {ok} ציטוטים אומתו ונכללו; {bad} נדחו והוסרו."),
    "en": dict(title="Literature Review Report", count="{} articles", question="Research question", table="Summary of articles",
               cols=["#", "Topic", "Title", "Authors", "Year", "Source"], article="Title",
               authors="Authors", source="Source", type="Study type", file="File",
               findings="Key findings", relevance="Relevance and caveats", quotes="Quotes from the article",
               pdf_page="PDF page", journal_page="journal p. {}", ok="✅ verified",
               note=("> **Quote verification:** every quote was automatically checked against the text of "
                     "the PDF page it is attributed to. Unverified quotes were removed."),
               summary="**Verification summary:** {ok} quotes verified and included; {bad} rejected and removed."),
}


DECIMAL, MINUS = "\x01", "\x02"  # placeholders that survive the final strip
MIN_QUOTE_CHARS = 20  # normalized length; shorter quotes are not specific enough to prove anything


def norm(text):
    """Normalize away layout noise, keep everything that carries meaning.

    Removed: case, whitespace, line breaks, punctuation, word hyphens and line-end
    hyphenation ("cost-\\neffective" == "cost effective").
    Kept: letters in any script (Latin, Hebrew, Greek...), digits, the decimal point
    (1.1 != 11), a minus before a number (-0.40 != 0.40) and comparison / percent
    signs (p<0.05 != p>0.05).
    """
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[−‐-—]", "-", t)          # unicode minus / dashes -> "-"
    t = re.sub(r"(?<=\d)\.(?=\d)", DECIMAL, t)            # decimal point
    t = re.sub(r"-(?=\s*\d)", MINUS, t)                   # minus sign / numeric range
    return re.sub(r"[^\w<>≤≥=±%" + DECIMAL + MINUS + r"]|_", "", t)


def md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(spec, pdf_dir, out):
    """Verify every quote in `spec` against its PDF page and write the Markdown report.

    Returns (verified_count, rejected_list). Raises FileNotFoundError for a missing PDF.
    """
    L = LABELS.get(spec.get("language", "he"), LABELS["he"])
    arts = spec["articles"]

    md = [f"# {L['title']} ({L['count'].format(len(arts))})", ""]
    if spec.get("question"):
        md += [f"**{L['question']}:** {spec['question']}", ""]
    md += [L["note"], "", f"## {L['table']}", "",
           "| " + " | ".join(L["cols"]) + " |", "|" + "---|" * len(L["cols"])]
    for i, a in enumerate(arts, 1):
        md.append("| " + " | ".join(md_cell(x) for x in
                  [i, a.get("topic", ""), a["title"], a.get("authors", ""), a.get("year", ""),
                   a.get("journal", "")]) + " |")
    md.append("")

    ok_n, rejected = 0, []
    for i, a in enumerate(arts, 1):
        path = os.path.join(pdf_dir, a["file"])
        if not os.path.isfile(path):
            raise FileNotFoundError(f"article {i}: PDF not found: {path}")
        with pymupdf.open(path) as doc:
            pages = [norm(p.get_text()) for p in doc]
        off = int(a.get("page_offset") or 0)
        md += [f"## {i}. {a.get('topic', a['title'])}", "",
               f"**{L['article']}:** {a['title']}  ", f"**{L['authors']}:** {a.get('authors', '')}  ",
               f"**{L['source']}:** {a.get('journal', '')}  "]
        if a.get("study_type"):
            md.append(f"**{L['type']}:** {a['study_type']}  ")
        if a.get("doi"):
            md.append(f"**DOI:** [{a['doi']}](https://doi.org/{a['doi']})  ")
        md += [f"**{L['file']}:** `{os.path.basename(a['file'])}`", "", f"### {L['findings']}", ""]  # no local paths
        md += [f"- {b}" for b in a.get("bullets", [])]
        if a.get("relevance"):
            md += ["", f"**{L['relevance']}:** {a['relevance']}"]
        md += ["", f"### {L['quotes']}", ""]
        kept = 0
        for q in a.get("quotes", []):
            page, text = int(q["page"]), q["text"]
            n = norm(text)
            if len(n) < MIN_QUOTE_CHARS:  # "65%" would match almost anywhere
                rejected.append({"article": i, "file": a["file"], "claimed_page": page,
                                 "found_on_pages": [], "reason": "too short to verify", "text": text})
                continue
            if 1 <= page <= len(pages) and n in pages[page - 1]:
                ok_n += 1
                kept += 1
                loc = f"{L['pdf_page']} {page}" + (f" ({L['journal_page'].format(page + off)})" if off else "")
                md += [f"> \"{text}\"", ">", f"> — {loc} · {L['ok']}", ""]
            else:
                found = [k + 1 for k, p in enumerate(pages) if n and n in p]
                rejected.append({"article": i, "file": a["file"], "claimed_page": page,
                                 "found_on_pages": found, "text": text})
        if not kept:
            md += ["_(—)_", ""]
        md += ["---", ""]

    md.append(L["summary"].format(ok=ok_n, bad=len(rejected)))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    rej_path = os.path.splitext(out)[0] + ".rejected.json"
    if rejected:
        with open(rej_path, "w", encoding="utf-8") as f:
            json.dump(rejected, f, ensure_ascii=False, indent=2)
    elif os.path.exists(rej_path):
        os.remove(rej_path)

    return ok_n, rejected


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--out", help="output .md (default: Research_Summary_Report.md next to the spec)")
    ap.add_argument("--pdf-dir", help="folder with the PDFs (overrides the spec's pdf_dir)")
    args = ap.parse_args()

    spec_dir = os.path.dirname(os.path.abspath(args.spec))
    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)
    pdf_dir = args.pdf_dir or spec.get("pdf_dir", "Research_PDFs")
    pdf_dir = pdf_dir if os.path.isabs(pdf_dir) else os.path.join(spec_dir, pdf_dir)
    out = args.out or os.path.join(spec_dir, "Research_Summary_Report.md")
    try:
        ok_n, rejected = build_report(spec, pdf_dir, out)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Report: {out}")
    print(f"Quotes verified: {ok_n}/{ok_n + len(rejected)}")
    for r in rejected:
        hint = (r.get("reason") or (f"actually on page(s) {r['found_on_pages']}" if r["found_on_pages"]
                                    else "not found anywhere in the PDF"))
        print(f"  REJECTED [article {r['article']}, p.{r['claimed_page']}] {hint}: {r['text'][:80]}...")
    sys.exit(2 if rejected else 0)


if __name__ == "__main__":
    main()
