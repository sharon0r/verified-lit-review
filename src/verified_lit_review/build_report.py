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
               why="למה נבחר", database="נמצא במאגר", route="הורד דרך",
               numbers="מספרים שנבדקו מול המאמר", found="נמצאו", not_found="לא עברו בדיקה",
               r_not_in_paper="לא מופיע במאמר", r_not_on_page="לא בעמוד שצוין",
               r_sign="הסימן שונה מהמאמר", r_ci_apart="האומדן ורווח הסמך לא מופיעים יחד",
               abstract_only="מתוך התקציר בלבד: לא אומת מול טקסט מלא. לא נכלל במספרי המסקנה ובקביעת הוודאות.",
               abstract_short="תקציר בלבד", abstract_loc="תקציר",
               trials_not_found="**חיפשנו לפי שם המחבר, ולא נמצאו:**",
               references="מקורות (בסגנון Vancouver)",
               references_note="_המאמרים שנקראו, ואחריהם המאמרים שלא ניתן היה לפתוח. יש להשלים כרך ועמודים לפני הגשה._",
               trust_ok="**נבדק בקוד:** {q} ציטוטים ו-{n} מספרים נבדקו מול המאמרים, וכולם נמצאו.",
               trust_some=("**נבדק בקוד:** {q_ok} מתוך {q} ציטוטים ו-{n_ok} מתוך {n} מספרים נמצאו במאמרים. "
                           "מה שלא נמצא מסומן ⚠️ בדוח."),
               fit="התאמה לשאלה", p_population="אוכלוסייה", p_intervention="התערבות",
               p_comparison="השוואה", p_outcome="תוצאה",
               overlap="חפיפה בין הסקירות",
               overlap_note=("_ניסויים שמוזכרים בשמם ובשנתם ביותר מסקירה אחת (בטבלה, בטקסט או ברשימת המקורות). "
                             "סביר שהסקירות חולקות אותם, ולכן אינן אישור בלתי תלוי זו של זו._"),
               overlap_item="מוזכר ב-{n} מתוך {of} הסקירות",
               start_high="רמת הפתיחה, שנקבעה בקוד לפי סוגי המחקרים: גבוהה (יש ניסויים אקראיים). כל הורדה מנומקת.",
               start_low="רמת הפתיחה, שנקבעה בקוד לפי סוגי המחקרים: נמוכה (מחקרים תצפיתיים בלבד).",
               auto_review="סקירה שיטתית או מטה-אנליזה שהסוכן בחר לקרוא, אך לא הייתה בגישה פתוחה.",
               auto_trial="ניסוי אקראי שהסוכן בחר לקרוא, אך לא היה בגישה פתוחה.",
               auto_found="ניסוי מרכזי שנמצא בחיפוש לפי שם המחבר, אך לא נקרא בסקירה הזאת.",
               missing_strong=("**חסרים מחקרים חזקים:** {failed} מתוך {total} המחקרים החזקים שהסוכן ניסה לפתוח "
                               "(סקירות שיטתיות וניסויים אקראיים) לא היו בגישה פתוחה. רמת הוודאות זמנית, "
                               "והמסקנה עלולה להשתנות כשיקראו אותם."),
               direction_warning=("**בדיקה אוטומטית:** בחלק מהמאמרים יש תוצאות מובהקות בכיוונים הפוכים, "
                                  "והמסקנה לא מציינת זאת. כדאי לבדוק את הממצאים מול המסקנה."),
               no_numbers="אין מספרים לבדיקה",
               conclusion_numbers="מספרים במסקנה שנבדקו מול הממצאים המאומתים", not_traced="לא מופיעים בממצאים",
               findings="עיקרי הממצאים", relevance="רלוונטיות והסתייגויות", quotes="ציטוטים מתוך המאמר",
               pdf_page="עמוד PDF", journal_page="עמוד {} בכתב העת", ok="✅ אומת",
               note=("> **מה מאומת:** כל **ציטוט** נבדק אוטומטית, מילה במילה, מול טקסט עמוד ה-PDF שאליו הוא משויך, "
                     "וציטוטים שלא אומתו הוסרו. **המספרים** בממצאים נבדקים מול המספרים שבמאמר, ומספר במסקנה "
                     "חייב להופיע בממצא או בציטוט מאומת. מספר שלא עבר בדיקה מסומן ⚠️. **הניסוח והפרשנות** הם סיכום שכתב מודל AI ואינם נבדקים אוטומטית. "
                     "יש לבדוק אותם מול המקור לפני כל שימוש מקצועי."),
               conclusion="מסקנה: מה הספרות אומרת על השאלה",
               certainty="ודאות הראיות (בסגנון GRADE, הערכה משוערת)",
               scope=("> **היקף:** {n} מאמרים בגישה פתוחה שבחר סוכן AI מתוך תוצאות החיפוש. זו סקירה ראשונית "
                      "ולא סקירה שיטתית: מאמרים שאינם בגישה פתוחה, וחלק מהסקירות המרכזיות בתחום, עשויים להיות חסרים."),
               bottom_line="בשורה התחתונה", papers="המאמרים בסקירה", how="איך נבדק הדו\"ח",
               not_read="מאמרים חשובים שלא ניתן היה לפתוח",
               not_read_note="_נמצאו בחיפוש אך לא הורדו (אין גישה פתוחה להורדה). לא נלקחו מהם ממצאים. מומלץ לקרוא בקישור._",
               why_important="למה חשוב",
               summary="**סיכום אימות:** {ok} ציטוטים אומתו ונכללו; {bad} נדחו והוסרו."),
    "en": dict(title="Literature Review Report", count="{} articles", question="Research question", table="Summary of articles",
               cols=["#", "Topic", "Title", "Authors", "Year", "Source"], article="Title",
               authors="Authors", source="Source", type="Study type", file="File",
               why="Why chosen", database="Found in", route="downloaded via",
               numbers="Numbers checked against the paper", found="found", not_found="failed the check",
               r_not_in_paper="not in the paper", r_not_on_page="not on the cited page",
               r_sign="sign differs from the paper", r_ci_apart="estimate and interval not printed together",
               abstract_only=("From the abstract only: not checked against the full text. Not used for the "
                              "conclusion's numbers or the certainty rating."),
               abstract_short="abstract only", abstract_loc="abstract",
               trials_not_found="**Searched by first author and not found:**",
               references="References (Vancouver style)",
               references_note="_Papers read, then papers that could not be opened. Add volume and pages before submission._",
               trust_ok="**Checked in code:** {q} quotes and {n} numbers were checked against the papers, and all were found.",
               trust_some=("**Checked in code:** {q_ok} of {q} quotes and {n_ok} of {n} numbers were found in the "
                           "papers. Anything not found is marked ⚠️ in the report."),
               fit="Fit to the question", p_population="population", p_intervention="intervention",
               p_comparison="comparison", p_outcome="outcome",
               overlap="Overlap between the reviews",
               overlap_note=("_Trials named, with their year, in more than one review (in a table, the text or the reference "
                             "list). The reviews likely share them, so they do not confirm each other independently._"),
               overlap_item="mentioned in {n} of the {of} reviews",
               start_high="Starting level, set in code from the study designs: high (randomized trials). Every step down is explained.",
               start_low="Starting level, set in code from the study designs: low (observational studies only).",
               auto_review="A systematic review or meta-analysis the agent chose to read, but it was not open access.",
               auto_trial="A randomized trial the agent chose to read, but it was not open access.",
               auto_found="A landmark trial found by its first author, but not read for this review.",
               missing_strong=("**Strong studies missing:** {failed} of the {total} strongest studies the agent "
                               "tried to open (systematic reviews, randomized trials) were not open access. The "
                               "certainty is provisional, and the conclusion may change once they are read."),
               direction_warning=("**Automatic check:** some papers report significant results in opposite "
                                  "directions, and the conclusion does not say so. Check the findings against "
                                  "the conclusion."),
               no_numbers="no numbers to check",
               conclusion_numbers="Numbers in the conclusion checked against the verified findings",
               not_traced="not in the findings",
               findings="Key findings", relevance="Relevance and caveats", quotes="Quotes from the article",
               pdf_page="PDF page", journal_page="journal p. {}", ok="✅ verified",
               note=("> **What is verified:** every **quote** was automatically checked, word for word, against the "
                     "text of the PDF page it is attributed to; unverified quotes were removed. **Numbers** in the "
                     "findings are checked against the numbers printed in the paper, and every number in the conclusion "
                     "must appear in a verified finding or quote; a number that fails is marked ⚠️. **Wording and interpretation** are an AI-written summary and are not "
                     "machine-checked; verify them against the source before professional use."),
               conclusion="Conclusion: what the literature says",
               certainty="Certainty of evidence (GRADE-style, approximate)",
               scope=("> **Scope:** {n} open-access papers chosen by an AI agent from the search results. This is a "
                      "rapid first look, not a systematic review: papers that are not open access, and some key "
                      "reviews in the field, may be missing."),
               bottom_line="Bottom line", papers="Papers in this review", how="How this report was checked",
               not_read="Important papers that could not be opened",
               not_read_note="_Found in the search but not downloadable. No findings were taken from them; read them at the link._",
               why_important="Why it matters",
               summary="**Verification summary:** {ok} quotes verified and included; {bad} rejected and removed."),
}


DECIMAL, MINUS = "\x01", "\x02"  # placeholders that survive the final strip
REASONS = {"not_in_paper", "not_on_page", "sign", "ci_apart"}   # numbers.check_finding problem kinds
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


def number_line(L, result, label="numbers", missing="not_found"):
    """'Numbers checked against the paper: 7/8 found · ⚠️ not found: 0.72' (or 'none to check')."""
    ok, bad = result.get("verified", []), result.get("unverified", [])
    if not ok and not bad:
        return f"_{L[label]}: {L['no_numbers']}_"
    line = f"_{L[label]}: {len(ok)}/{len(ok) + len(bad)} {L['found']}_"
    why = result.get("reasons", {})
    shown = [f"{n} ({L['r_' + why[n]]})" if why.get(n) in REASONS else n for n in bad]
    return line + (f" · ⚠️ **{L[missing]}:** {', '.join(shown)}" if bad else " ✅")


def shorten(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def trust_line(L, quotes_ok, quotes, arts, spec):
    """One line under the bottom line: what the code checked and whether everything was found
    (UX review: eleven per-paper check lines were not read; one line is)."""
    results = [a.get("numbers") or {} for a in arts] + [spec.get("conclusion_numbers") or {}]
    n_ok = sum(len(r.get("verified", [])) for r in results)
    n_all = n_ok + sum(len(r.get("unverified", [])) for r in results)
    if quotes_ok == quotes and n_ok == n_all:
        return f"> ✅ {L['trust_ok'].format(q=quotes, n=n_all)}"
    return f"> ⚠️ {L['trust_some'].format(q_ok=quotes_ok, q=quotes, n_ok=n_ok, n=n_all)}"


def paper_url(a):
    """A link a reader can click: the DOI resolver, else the PMC article page, else none."""
    if a.get("doi"):
        return f"https://doi.org/{a['doi']}"
    if a.get("pmcid"):
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{a['pmcid']}/"
    return a.get("url") or ""


def linked_title(a):
    title = " ".join(str(a["title"]).split())              # no line breaks inside a link
    title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    url = paper_url(a)
    return f"[{title}]({url})" if url else title


def short_cite(a):
    """'Mekonnen et al., 2016' from 'Mekonnen AB, McLachlan AJ' or 'Lauren Dautzenberg, Lisa ...'."""
    first = str(a.get("authors") or "").split(",")[0].strip().rstrip(".")
    words = first.split()
    if words:
        # "Mekonnen AB" -> surname first; "Lauren Dautzenberg" -> surname last
        name = words[0] if len(words) > 1 and re.fullmatch(r"[A-Z]{1,3}", words[-1]) else words[-1]
        first = name + (" et al." if "," in str(a.get("authors")) else "")
    return ", ".join(x for x in [first, str(a.get("year") or "")] if x)


def vancouver_authors(authors, limit=6):
    """'Elsa Afonso, Koen Blot' or 'Afonso E, Blot K' -> 'Afonso E, Blot K'; more than 6 -> 6 + 'et al.'"""
    names, more = [], False
    for raw in str(authors or "").split(","):
        n = " ".join(raw.replace(".", " ").split())
        if not n:
            continue
        if n.lower().startswith("et al"):
            more = True
            continue
        words = n.split()
        if len(words) > 1 and re.fullmatch(r"[A-Z]{1,3}", words[-1]):
            names.append(n)                                   # already 'Surname AB'
        elif len(words) > 1:                                  # 'Elsa M Afonso' -> 'Afonso EM'
            names.append(words[-1] + " " + "".join(w[0].upper() for w in words[:-1] if w[0].isalpha()))
        else:
            names.append(n)
    if len(names) > limit:
        names, more = names[:limit], True
    return ", ".join(names) + (", et al." if more and names else "." if names else "")


def vancouver(a):
    """One reference in Vancouver style from what the search records hold (no volume or pages)."""
    title = " ".join(str(a.get("title") or "").split()).rstrip(".")
    parts = [vancouver_authors(a.get("authors")), f"{title}.", f"{short_journal(a.get('journal'))}."
             if a.get("journal") else "", f"{a.get('year')}." if a.get("year") else ""]
    ref = " ".join(p for p in parts if p)
    if a.get("doi"):
        ref += f" doi:{a['doi']}"
    elif paper_url(a):
        ref += f" {paper_url(a)}"
    return ref


def cite_link(a):
    cite = short_cite(a) or " ".join(str(a.get("title", "")).split()[:6])
    url = paper_url(a)
    return f"[{cite}]({url})" if url else cite


def short_journal(name):
    """'Euro surveillance : bulletin Europeen ... = European ...' -> 'Euro surveillance'. PubMed gives
    the full catalogue name, which wraps over three lines on a phone (UX review)."""
    j = re.split(r"\s+[:=]\s+", " ".join(str(name or "").split()))[0]
    return re.sub(r"^The\s+", "", j)


# "trad- itional", "cross-in- fection": line-end hyphenation left in the PDF text. Joined for display
# only; the quote is verified as written. "short- and long-term" keeps its hyphen.
_PDF_HYPHEN = re.compile(r"(?<=[a-z])- (?!(?:and|or|to|nor|vs|versus)\b)(?=[a-z])")


def clean_decimals(text):
    """'3⋅99' (Lancet's middle-dot decimal) -> '3.99' (same rule as numbers.clean_decimals; this file
    also runs as a stand-alone script, so it cannot import the package)."""
    return re.sub(r"(?<=\d)[⋅·∙](?=\d)", ".", text or "")


def display_quote(text):
    """'sig-\\nnificant' -> 'significant', '3⋅99' -> '3.99', PDF line breaks -> spaces."""
    return _PDF_HYPHEN.sub("", clean_decimals(" ".join(str(text).split())))


def md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(spec, pdf_dir, out):
    """Verify every quote in `spec` against its PDF page and write the Markdown report.

    Returns (verified_count, rejected_list). Raises FileNotFoundError for a missing PDF.
    """
    L = LABELS.get(spec.get("language", "he"), LABELS["he"])
    arts = spec["articles"]

    # The answer first, then the papers, and how it was checked at the end: a hospital reader on a
    # phone should not scroll past metadata and caveats to find the bottom line (UX review).
    md = [f"# {L['title']} ({L['count'].format(len(arts))})", ""]
    if spec.get("question"):
        md += [f"**{L['question']}:** {spec['question']}", ""]
    if spec.get("bottom_line"):
        md += [f"> **{L['bottom_line']}:** {clean_decimals(spec['bottom_line'])}", ""]
    banner_at = len(md)                 # one trust line, filled in once every quote has been checked
    if spec.get("conclusion"):
        md += [f"## {L['conclusion']}", "", clean_decimals(spec["conclusion"]), ""]
        if (spec.get("conclusion_numbers") or {}).get("unverified"):
            md += [number_line(L, spec["conclusion_numbers"], "conclusion_numbers", "not_traced"), ""]
        if spec.get("direction_warning"):
            md += [f"> ⚠️ {L['direction_warning']}", ""]
    if spec.get("certainty"):
        md += [f"**{L['certainty']}:** {clean_decimals(spec['certainty'])}", ""]
    failed, read = (spec.get("missing_strong") or [0, 0])
    if failed > read:
        md += [f"> ⚠️ {L['missing_strong'].format(failed=failed, total=failed + read)}", ""]
    md += [f"## {L['papers']}", ""]
    for i, a in enumerate(arts, 1):
        md.append(f"{i}. **{a.get('topic', '')}** — {cite_link(a)}"
                  + (f" · {short_journal(a['journal'])}" if a.get("journal") else "")
                  + (f" · 📄 {L['abstract_short']}" if a.get("abstract_only") else ""))
    md.append("")
    if spec.get("not_read") or spec.get("trials_not_found"):
        md += [f"## {L['not_read']}", "", L["not_read_note"], ""]
        for n in spec["not_read"]:
            why = n.get("why_important") or L.get(f"auto_{n.get('auto')}", "")
            md.append(f"- {linked_title(n)} ({short_cite(n)}) — **{L['why_important']}:** {why}")
        if spec.get("trials_not_found"):
            md.append(f"- {L['trials_not_found']} {', '.join(spec['trials_not_found'])}")
        md.append("")

    ok_n, rejected = 0, []
    for i, a in enumerate(arts, 1):
        if a.get("abstract_only"):      # no open PDF: checked against the abstract, shown as such
            pages = {0: norm(a.get("abstract") or "")}
        else:
            path = os.path.join(pdf_dir, a["file"])
            if not os.path.isfile(path):
                raise FileNotFoundError(f"article {i}: PDF not found: {path}")
            with pymupdf.open(path) as doc:
                pages = {k: norm(p.get_text()) for k, p in enumerate(doc, 1)}
        off = int(a.get("page_offset") or 0)
        source = " · ".join(str(x) for x in [short_cite(a), short_journal(a.get("journal"))] if x)
        md += [f"## {i}. {a.get('topic', a['title'])}", "",
               f"**{L['article']}:** {linked_title(a)}  ", f"**{L['source']}:** {source}  "]
        if a.get("abstract_only"):
            md.append(f"📄 **{L['abstract_only']}**  ")
        if a.get("study_type"):
            md.append(f"**{L['type']}:** {a['study_type']}  ")
        if a.get("why_chosen"):
            md.append(f"**{L['why']}:** {a['why_chosen']}  ")
        misfit = [(p, (a.get("pico_fit") or {}).get(p) or {}) for p in ("population", "intervention",
                                                                       "comparison", "outcome")]
        misfit = [(p, f) for p, f in misfit if not f.get("match", True)]
        if misfit:                      # only what does not fit the question, in a few words
            md.append(f"**{L['fit']}:** " + " · ".join(
                f"⚠️ {L['p_' + p]}: {shorten(f.get('studied', ''), 45)}" for p, f in misfit) + "  ")
        md += ["", f"### {L['findings']}", ""]
        md += [f"- {clean_decimals(b)}" for b in a.get("bullets", [])]
        if (a.get("numbers") or {}).get("unverified"):
            md += ["", number_line(L, a["numbers"])]
        if a.get("relevance"):
            md += ["", f"**{L['relevance']}:** {clean_decimals(a['relevance'])}"]
        md += ["", f"### {L['quotes']}", ""]
        kept = 0
        for q in a.get("quotes", []):
            page, text = int(q["page"]), q["text"]
            n = norm(text)
            if len(n) < MIN_QUOTE_CHARS:  # "65%" would match almost anywhere
                rejected.append({"article": i, "file": a["file"], "claimed_page": page,
                                 "found_on_pages": [], "reason": "too short to verify", "text": text})
                continue
            if page in pages and n in pages[page]:
                ok_n += 1
                kept += 1
                loc = (L["abstract_loc"] if a.get("abstract_only") else f"{L['pdf_page']} {page}"
                       + (f" ({L['journal_page'].format(page + off)})" if off else ""))
                md += [f"> \"{display_quote(text)}\"", ">", f"> — {loc} · {L['ok']}", ""]
            else:
                found = [k for k, p in pages.items() if n and n in p]
                rejected.append({"article": i, "file": a["file"], "claimed_page": page,
                                 "found_on_pages": found, "text": text})
        if not kept:
            md += ["_(—)_", ""]
        md += ["---", ""]

    refs = arts + list(spec.get("not_read") or [])
    md += [f"## {L['references']}", "", L["references_note"], ""]
    md += [f"{i}. {vancouver(a)}" for i, a in enumerate(refs, 1)] + [""]
    md += [f"## {L['how']}", "", L["summary"].format(ok=ok_n, bad=len(rejected)), ""]
    if spec.get("overlap"):
        md += [f"**{L['overlap']}** — {L['overlap_note']}", ""]
        for o in spec["overlap"]:
            md.append(f"- **{o['trial']}:** " + L["overlap_item"].format(n=len(o["reviews"]), of=o["of"])
                      + f" ({'; '.join(o['reviews'])})")
        md.append("")
    if spec.get("certainty_start") in ("high", "low"):
        md += [f"_{L['start_' + spec['certainty_start']]}_", ""]
    md += [L["note"], "", L["scope"].format(n=len(arts)), ""]
    if spec.get("bottom_line"):
        md[banner_at:banner_at] = [trust_line(L, ok_n, ok_n + len(rejected), arts, spec), ""]
    if spec.get("pico"):
        md += [f"**PICO:** {spec['pico']}", ""]
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
