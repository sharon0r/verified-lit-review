# -*- coding: utf-8 -*-
"""
Second guardrail: every number in the AI-written findings must appear in the paper.

Quotes are checked word for word (build_report.norm). Findings are the model's own words, so they
cannot be matched as text - but the numbers in them can. Each number in a finding (an effect size,
a confidence limit, a percentage, a sample size) is looked up among the numbers printed anywhere in
the paper. A rounded value counts as found (0.72 for a printed 0.719); a number the paper does not
contain is reported as unverified and shown with a warning in the report.

What is not checked: small integers (0-9, too common to mean anything), years, the sign of a value
(a printed minus sign is often a different character), and whether the number is used in the right
context. In qualitative studies with few numbers there may be nothing to check.
"""
import re

NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.,])\d+(?:\.\d+)?(?![\w])")


def _canon(token):
    return token.replace(",", "")


def extract(text):
    """Meaningful numbers in a piece of text, as canonical strings ('1602', '0.69', '33.8')."""
    out = []
    for m in NUM.finditer(text or ""):
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
            self.exact.update(_canon(m.group()) for m in NUM.finditer(t or ""))
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
