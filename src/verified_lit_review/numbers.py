# -*- coding: utf-8 -*-
"""
Second guardrail: every number in the AI-written findings must appear in the paper.

Quotes are checked word for word (build_report.norm). Findings are the model's own words, so they
cannot be matched as text - but the numbers in them can. Each number in a finding (an effect size,
a confidence limit, a percentage, a sample size) is looked up among the numbers printed anywhere in
the paper. A rounded value counts as found (0.72 for a printed 0.719); a number the paper does not
contain is reported as unverified and shown with a warning in the report.

Findings are checked more strictly (check_finding): each number must be printed on the page(s) the
finding cites, a minus sign must match the paper, and an estimate with its confidence interval must be
printed together on that page.

What is not checked: small integers (0-9, too common to mean anything), years, numbers written as
words, and whether a number printed on the right page belongs to the outcome the finding names.
In qualitative studies with few numbers there may be nothing to check.
"""
import re

NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.,])\d+(?:\.\d+)?(?![\w])")
# A decimal comma, as in Spanish or German papers: "23,2", "2,86", "p=0,034". A report written with
# a decimal point ("23.2") must still find these; three digits after the comma ("1,000") stay thousands
# unless the number starts with 0.
DECIMAL_COMMA = re.compile(r"(?<![\w.,])(?:\d+,\d{1,2}|0,\d+)(?![\w,])")


MID_DOT = re.compile(r"(?<=\d)[⋅·∙](?=\d)")


def clean_decimals(text):
    """'3⋅99' (Lancet's middle-dot decimal) -> '3.99'. Same length, so positions stay valid."""
    return MID_DOT.sub(".", text or "")


def _canon(token):
    return token.replace(",", "")


def extract(text):
    """Meaningful numbers in a piece of text, as canonical strings ('1602', '0.69', '33.8')."""
    out = []
    for m in NUM.finditer(clean_decimals(text)):
        s = _canon(m.group())
        value = float(s)
        if "." not in s and (value < 10 or 1900 <= value <= 2100):
            continue                     # tiny integers and years carry no checkable claim
        if s not in out:
            out.append(s)
    return out


class PaperNumbers:
    """All numbers printed in one paper, for exact and rounded lookup."""

    def __init__(self, page_texts):
        self.exact = set()
        for t in page_texts:
            t = clean_decimals(t)
            self.exact.update(_canon(m.group()) for m in NUM.finditer(t or ""))
            self.exact.update(m.group().replace(",", ".") for m in DECIMAL_COMMA.finditer(t or ""))
        self.floats = [float(s) for s in self.exact]

    def contains(self, number):
        if number in self.exact:
            return True
        if "." in number:                # accept a value the paper prints with more decimals
            digits = len(number.split(".")[1])
            target = float(number)
            return any(round(f, digits) == target for f in self.floats)
        return False


def check(text, paper):
    """Returns (verified, unverified) lists of the numbers in `text`."""
    nums = extract(text)
    found = [n for n in nums if paper.contains(n)]
    return found, [n for n in nums if n not in found]


# ------------------------------------------------------------------ page-level check of one finding
# A number that exists somewhere in a paper can still be the wrong number: a secondary outcome's value
# given as the primary one, or a value from another table. So a finding names the page(s) its numbers
# come from, and each number must be on those pages, with the same sign, and an estimate with its
# confidence interval must be printed together there.
MINUS_SIGNS = "-−‐–"
ESTIMATE_GAP = 60     # characters from an estimate to its interval's low limit on the page
LIMIT_GAP = 30        # characters from the low limit to the high limit
# "0.74 (95% CI 0.60 to 0.90)", "IRR 0.59 (0.52-0.68)", "0.74 (רווח סמך 95% 0.60 עד 0.90)"
CI = re.compile(r"(?<![\w.])(?P<x>\d+(?:\.\d+)?)\s*%?\s*\((?:[^()\d]{0,40}?95\s*%)?[^()\d]{0,15}?"
                r"(?P<a>[-−‐]?\d+(?:\.\d+)?)\s*%?\s*(?:–|—|-|to|עד)\s*(?P<b>[-−‐]?\d+(?:\.\d+)?)")


CI_LABEL = re.compile(r"95\s*%\s*(?:CI\b|C\.I\.|confidence interval|רווח סמך|רווח בר-סמך)"
                      r"|(?:רווח סמך|רווח בר-סמך|confidence interval)\s*(?:של\s*)?95\s*%", re.IGNORECASE)


def _negative(text, start):
    """Is the number at `start` preceded by a minus sign (not a range dash as in '0.60-0.94')?"""
    i = start - 1
    if i < 0 or text[i] not in MINUS_SIGNS:
        return False
    j = i - 1
    while j >= 0 and text[j] == " ":
        j -= 1
    if j < 0:
        return True
    if j < i - 1:                   # a space before the sign: "to -0.06" is negative, "0.60 - 0.94" a range
        return not (text[j].isdigit() or text[j] in "%).]")
    return not (text[j].isalnum() or text[j] in "%).]")    # "COVID-19", "0.60-0.94": no minus


def tokens(text):
    """[(canonical, value, negative, position)] for every number printed in `text`."""
    text = clean_decimals(text)
    out = []
    for m in NUM.finditer(text or ""):
        s = _canon(m.group())
        out.append((s, float(s), _negative(text, m.start()), m.start()))
    for m in DECIMAL_COMMA.finditer(text or ""):
        s = m.group().replace(",", ".")
        out.append((s, float(s), _negative(text, m.start()), m.start()))
    return out


def same(printed, number):
    """The paper's printed value supports `number` exactly or rounded (0.719 supports 0.72)."""
    if printed[0] == number:
        return True
    if "." in number:
        return round(printed[1], len(number.split(".")[1])) == float(number)
    return False


class PageIndex:
    """The numbers of one paper, page by page, with sign and position."""

    def __init__(self, pages):                  # {page number: page text}
        self.pages = {p: tokens(t) for p, t in pages.items()}

    def on(self, page, number, negative=None):
        return [t for t in self.pages.get(page, []) if same(t, number) and (negative is None or t[2] == negative)]

    def together(self, page, x, a, b):
        """The estimate is printed right before its interval: low limit within ESTIMATE_GAP characters
        after it, high limit within LIMIT_GAP after that. On 475 real estimates from cached papers this
        passed all correct ones and caught 96.8% of one-digit changes (a looser 250-character window
        caught 90.1%: in dense tables a wrong value is often printed somewhere near the interval)."""
        toks = self.pages.get(page, [])
        for tx in self.on(page, x):
            for ta in toks:
                if 0 < ta[3] - tx[3] <= ESTIMATE_GAP and same(ta, a):
                    if any(0 < tb[3] - ta[3] <= LIMIT_GAP and same(tb, b) for tb in toks):
                        return True
        return False


def check_finding(text, cited_pages, index):
    """Check one AI-written finding against the pages it cites.

    Returns (verified, unverified, problems): problems are (number, kind, detail) with kind one of
    'not_in_paper', 'not_on_page', 'sign', 'ci_apart'. With no cited pages, the whole paper counts."""
    all_pages = sorted(index.pages)
    scope = [p for p in (cited_pages or []) if p in index.pages] or all_pages
    text = CI_LABEL.sub(lambda m: " " * len(m.group()), clean_decimals(text))   # "95% CI": a label, not a result
    found, problems = [], []
    seen = set()
    for m in NUM.finditer(text or ""):
        n = _canon(m.group())
        if n in seen or ("." not in n and (float(n) < 10 or 1900 <= float(n) <= 2100)):
            continue
        seen.add(n)
        on = [p for p in scope if index.on(p, n)]
        if not on:
            elsewhere = [p for p in all_pages if index.on(p, n)]
            problems.append((n, "not_on_page", f"printed on page(s) {elsewhere}, not on the cited page(s) "
                                               f"{cited_pages}") if elsewhere else (n, "not_in_paper", "not printed in the paper"))
            continue
        if _negative(text, m.start()) and not any(index.on(p, n, negative=True) for p in on):
            problems.append((n, "sign", "written with a minus sign, but the paper prints it without one"))
            continue
        found.append(n)
    for m in CI.finditer(text or ""):
        x, a, b = m.group("x"), m.group("a").lstrip("-−‐"), m.group("b").lstrip("-−‐")
        if {x, a, b} <= set(found) and not any(index.together(p, x, a, b) for p in scope):
            problems.append((x, "ci_apart", f"estimate {x} and interval {a} to {b} are not printed together "
                                            f"on page(s) {scope}"))
            found.remove(x)
    bad = []
    for n, _, _ in problems:
        if n not in bad:
            bad.append(n)
    return found, bad, problems
