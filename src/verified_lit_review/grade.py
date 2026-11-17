# -*- coding: utf-8 -*-
"""
Fourth guardrail: the certainty rating has a base in code.

GRADE starts from the design: a body of evidence with randomized trials starts HIGH, one with only
observational studies starts LOW. Each level below the start needs a named reason (risk of bias,
inconsistency, indirectness, imprecision, publication bias); a level above the start needs an explicit
reason for upgrading (a large effect, a dose-response gradient). The model still judges - the code
checks that the judgement is explained and does not start from the wrong place.

Designs are read from the paper's title and the agent's own `study_type`. A paper whose design is not
recognised does not set the start; when no design is recognised (qualitative or laboratory evidence,
for example), the rating is not checked.
"""
import re

LEVELS = ["very low", "low", "moderate", "high"]
LEVEL_WORDS = [("very low", r"very low|נמוכה מאוד|נמוכה-מאוד"), ("low", r"low|נמוכה"),
               ("moderate", r"moderate|בינונית"), ("high", r"high|גבוהה")]
LEVEL_AT_START = re.compile(r"^\W*(" + "|".join(w for _, w in LEVEL_WORDS) + r")", re.IGNORECASE)

RANDOMIZED = re.compile(r"randomi[sz]|\bRCTs?\b|controlled trial|cluster[- ]randomi|crossover trial|"
                        r"ניסוי(?:ים)? (?:קליני(?:ים)? )?(?:אקראי|מבוקר)|אקראי(?:ים)? מבוקר|באקראי|ניסוי אשכולות",
                        re.IGNORECASE)
OBSERVATIONAL = re.compile(r"cohort|case[- ]control|cross[- ]sectional|observational|before[- ]and[- ]after|"
                           r"before-after|quasi[- ]experiment|registry|retrospective|survey|"
                           r"עוקבה|קוהורט|מקרה-ביקורת|חתך|תצפיתי|לפני-אחרי|מעין[- ]ניסוי|רטרוספקטיבי|סקר",
                           re.IGNORECASE)
DOMAINS = {
    "risk of bias": r"risk of bias|bias|blind|allocation|concealment|attrition|drop-?out|loss to follow|"
                    r"הטיה|סמיות|סמויים|הקצאה|נשירה|אובדן למעקב",
    "inconsistency": r"inconsisten|heterogen|I2|I²|חוסר עקביות|הטרוגניות|אי[- ]עקביות",
    "indirectness": r"indirect|עקיפות|עקיף|לא ישיר",
    "imprecision": r"imprecis|wide (?:confidence )?interval|crosses 1|few events|optimal information|TSA|"
                   r"חוסר דיוק|רווח(?:י)? סמך רחב|חוצה את 1|מעט אירועים|גודל המידע",
    "publication bias": r"publication bias|funnel|small[- ]study|הטיית פרסום|משפך",
}
DOUBLE = re.compile(r"two levels|very serious|שתי דרגות|פעמיים|חמור מאוד|הורדה כפולה", re.IGNORECASE)
UPGRADE = re.compile(r"upgrad|large effect|dose[- ]response|הועלה|העלאה|השפעה גדולה|יחס מינון", re.IGNORECASE)


def design(text):
    """'randomized', 'observational' or None for one paper (title + study type)."""
    if RANDOMIZED.search(text or ""):
        return "randomized"
    if OBSERVATIONAL.search(text or ""):
        return "observational"
    return None


def start_level(designs):
    """GRADE start for the body of evidence: high with any randomized evidence, low with only
    observational evidence, None when no design is recognised."""
    known = [d for d in designs if d]
    if not known:
        return None
    return "high" if "randomized" in known else "low"


def rated_level(certainty):
    m = LEVEL_AT_START.match(certainty or "")
    if not m:
        return None
    word = m.group(1).lower()
    return next(level for level, w in LEVEL_WORDS if re.fullmatch(w, word, re.IGNORECASE))


def check(certainty, designs):
    """Returns (start, problem): problem is None when the rating is explained."""
    start = start_level(designs)
    if start is None or not (certainty or "").strip():
        return start, None
    level = rated_level(certainty)
    if level is None:
        return start, ("start `certainty` with the level (high / moderate / low / very low; in Hebrew "
                       "גבוהה / בינונית / נמוכה / נמוכה מאוד) for the main outcome")
    steps = LEVELS.index(start) - LEVELS.index(level)
    if steps < 0 and not UPGRADE.search(certainty):
        return start, (f"the evidence starts at {start} (its designs), but the rating is {level}: rating above "
                       "the start needs an explicit reason for upgrading (large effect, dose-response)")
    if steps > 0:
        named = [d for d, pattern in DOMAINS.items() if re.search(pattern, certainty, re.IGNORECASE)]
        counted = len(named) + (1 if DOUBLE.search(certainty) else 0)
        if counted < steps:
            return start, (f"the evidence starts at {start} (its designs) and is rated {level}: {steps} level(s) "
                           f"down, but only {len(named)} reason(s) named ({', '.join(named) or 'none'}). Name a "
                           "reason for each level: risk of bias, inconsistency, indirectness, imprecision, "
                           "publication bias")
    return start, None
