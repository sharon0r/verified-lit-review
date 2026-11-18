# -*- coding: utf-8 -*-
"""
Third guardrail: the words must fit the numbers.

1. Enforced (the report is sent back): an estimate whose confidence interval includes no effect
   (1 for a ratio such as RR, OR, HR; 0 for a difference) may not sit in the same clause as a plain
   effect verb ("reduced", "הפחית"). "Did not significantly reduce" and "may reduce" are allowed.
2. Warning only (shown in the report, not sent back): the papers show significant effects in opposite
   directions and the conclusion does not say that they disagree. Words carry direction loosely - a
   ratio above 1 can be good or bad news - so this is a prompt for a human to look, not a verdict.

Only estimates written with their interval are read: "RR 0.81 (0.65 to 1.01)",
"(יחס שיעורים 0.60, רווח סמך 95% 0.34 עד 1.04)". The kind of estimate (ratio or difference) comes from
its label just before it; an estimate with no recognisable label is not judged.
"""
import re

from .numbers import clean_decimals

LABEL ="§"                        # placeholder for an interval label ("95% CI", "רווח סמך 95%")
CI_WORDS = re.compile(r"95\s*%\s*(?:CI\b|C\.I\.|confidence intervals?|רווח(?:\s*בר-?)?\s*סמך)"
                      r"|(?:רווח(?:\s*בר-?)?\s*סמך|confidence intervals?)\s*(?:של\s*)?(?:95\s*%)?|\bCI\b",
                      re.IGNORECASE)
NUMBER = r"[-−‐]?\d+(?:\.\d+)?"
ESTIMATE = re.compile(rf"(?<![\w.])(?P<x>{NUMBER})\s*%?(?:\s+[^\W\d_]{{1,12}}){{0,2}}\s*(?:\(\s*{LABEL}?|[,;]\s*{LABEL}|{LABEL})[\s{LABEL}]*"
                      rf"(?:(?:בין|between)\s*)?(?P<a>{NUMBER})\s*%?\s*(?:–|—|-|to|and|עד|ל-|ל)\s*"
                      rf"(?P<b>{NUMBER})")
RATIO = re.compile(r"\b(?:a?RR|a?OR|a?HR|IRR|RRR|risk ratio|odds ratio|hazard ratio|rate ratio|ratio)\b|יחס",
                   re.IGNORECASE)
DIFFERENCE = re.compile(r"\b(?:S?MD|WMD|RD|ARD|ES|mean difference|risk difference|difference|effect size|"
                        r"Hedges'? ?g|Cohen'?s d)\b|הפרש|הבדל|גודל אפקט|גודל השפעה",
                        re.IGNORECASE)
# a plain claim of an effect; nouns ("reduction") and hedged forms are not matched
EFFECT_EN = re.compile(r"\b(?:reduc(?:es|ed)|reduce|lower(?:s|ed)|decreas(?:es|ed)|increas(?:es|ed)|"
                       r"improv(?:es|ed)|prevent(?:s|ed)|raise[sd]?)\b", re.IGNORECASE)
EFFECT_HE = re.compile(r"(?:^|[\s\"'(])[ושה]?(?:מפחית|הפחית|מורידה?|הוריד|מקטינה?|הקטין|מגדילה?|הגדיל|"
                       r"משפרת?|שיפר|העלה|העלתה|מונעת?|מנע)\w*")
# negation or hedging in the same clause makes the verb acceptable
SOFT = re.compile(r"\b(?:not|no|non-?significant(?:ly)?|without|may|might|could|uncertain|unclear)\b|"
                  r"(?:^|\s)(?:לא|אין|ללא|עשוי|עשויה|עשויים|עשויות|ייתכן|אולי|ספק)(?:\s|$|,)",
                  re.IGNORECASE)
DISAGREE = re.compile(r"\b(?:inconsistent|conflicting|disagree|contradict|opposite|mixed)\w*|"
                      r"סותר|סתירה|לא עקבי|מנוגד|הפוך|הפוכים|חלוקות|חלוקים|לא אחיד", re.IGNORECASE)
CLAUSE_BREAK = re.compile(r"(?<!\d)[.;!?](?!\d)|,?\s+(?:but|whereas|while|although|however)\s+|"
                          r",?\s*(?:אך|אבל|ואילו|בעוד|לעומת זאת|עם זאת)\s+", re.IGNORECASE)


# "3.99 vs 3.45 per 1,000 days": two arms compared. A reader must know which arm had which value -
# a physician reviewer could not tell whether a trial's point estimate leaned toward harm.
COMPARE = re.compile(rf"(?<![\w.])(?P<x>{NUMBER})\s*%?[^\d\n]{{0,25}}?\s(?:לעומת|vs\.?|versus|compared with)\s+"
                     rf"[^\d\n]{{0,25}}?(?P<y>{NUMBER})", re.IGNORECASE)
ARMS = re.compile(r"intervention|control|placebo|usual care|standard care|sham|comparator|treated|untreated|"
                  r"before|after|baseline|arm|group|"
                  r"התערבות|ביקורת|בקרה|פלצבו|טיפול רגיל|טיפול המקובל|זרוע|קבוצ|לפני|אחרי", re.IGNORECASE)


def unnamed_arms(text, arm_words=()):
    """Comparisons of two values ('3.99 vs 3.45') with no arm named near them. arm_words: words that
    name this paper's arms (from its PICO intervention and comparison), e.g. 'chlorhexidine', 'soap'."""
    extra = [w for w in arm_words if len(w) >= 4]
    text = clean_decimals(text)
    found = []
    for m in COMPARE.finditer(text or ""):
        near = text[max(0, m.start() - 80):m.end() + 60]
        if ARMS.search(near) or any(w.lower() in near.lower() for w in extra):
            continue
        found.append(m.group().strip())
    return found


SECONDARY = re.compile(r"secondary|משני|משנית", re.IGNORECASE)
EXPLORATORY = re.compile(r"exploratory|hypothesis|גישוש|השערה", re.IGNORECASE)


def unlabelled_secondary(text):
    """A finding about a secondary outcome that does not call itself exploratory. A secondary result
    of a trial whose primary outcome was null was presented as a finding in a real report."""
    return bool(SECONDARY.search(text or "")) and not EXPLORATORY.search(text or "")


def _value(s):
    return float(s.replace("−", "-").replace("‐", "-"))


def estimates(text):
    """[{x, a, b, kind, null, start, end}] for each estimate written with its interval."""
    marked = CI_WORDS.sub(lambda m: LABEL + " " * (len(m.group()) - 1), clean_decimals(text))
    out = []
    for m in ESTIMATE.finditer(marked):
        before = marked[max(0, m.start() - 40):m.start()]
        kind = ("ratio" if RATIO.search(before) else "difference" if DIFFERENCE.search(before) else None)
        if kind is None:
            continue
        x, a, b = (_value(m.group(k)) for k in ("x", "a", "b"))
        low, high = min(a, b), max(a, b)
        null = 1.0 if kind == "ratio" else 0.0
        if kind == "ratio" and (x <= 0 or low <= 0):
            continue                                   # not a ratio after all (a misread label)
        out.append({"x": x, "a": low, "b": high, "kind": kind, "no_effect": null,
                    "null": low <= null <= high, "start": m.start(), "end": m.end()})
    return out


def _clause(text, start, end):
    """The clause around [start, end): from the last clause break before it to the next one after it."""
    s = 0
    for m in CLAUSE_BREAK.finditer(text):
        if m.end() <= start:
            s = m.end()
        elif m.start() >= end:
            return text[s:m.start()]
    return text[s:]


def overclaims(text):
    """Clauses that state an effect (plain verb, no negation or hedge) about an estimate whose
    interval includes no effect. Returns the clauses, trimmed."""
    found = []
    for e in estimates(text):
        if not e["null"]:
            continue
        clause = _clause(text, e["start"], e["end"])
        if (EFFECT_EN.search(clause) or EFFECT_HE.search(clause)) and not SOFT.search(clause):
            c = " ".join(clause.split())
            if c not in found:
                found.append(c)
    return found


def direction_warnings(paper_texts, conclusion):
    """Warn when significant estimates point in opposite directions across papers and the conclusion
    does not say the results disagree. paper_texts: {paper_id: text of its findings}."""
    down, up = set(), set()
    for pid, text in paper_texts.items():
        for e in estimates(text):
            # only ratios: a difference's sign depends on which arm is subtracted from which (a real
            # Cochrane abstract gave 'rate difference 1.70' for fewer infections)
            if e["null"] or e["kind"] != "ratio":
                continue
            (down if e["b"] < e["no_effect"] else up).add(pid)
    if down and up and (down - up or up - down) and not DISAGREE.search(conclusion or ""):
        return [f"significant effects in opposite directions (lower in {sorted(down)}, higher in "
                f"{sorted(up)}); the conclusion does not say the results disagree"]
    return []
