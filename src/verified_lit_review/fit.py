# -*- coding: utf-8 -*-
"""
Fit to the question (PICO) and overlap between reviews.

PICO fit: for each paper the agent states whether its population, intervention, comparison and outcome
match the question. The code checks two things it can see for itself: a paper that mentions children
cannot be marked as matching an adult question, and any mismatch must be named as indirectness in the
certainty rating.

Overlap: reviews often pool the same trials, so "four reviews agree" may be one body of evidence
counted four times. The agent lists the trials each review includes; the code keeps only names that
appear in that review's own text and reports trials included in more than one review.
"""
import re

from .grade import DOMAINS

PARTS = ["population", "intervention", "comparison", "outcome"]
CHILDREN = re.compile(r"\b(?:child|children|paediatric|pediatric|neonat\w*|infant\w*|adolescent\w*)\b|"
                      r"ילד|ילדים|פעוט|יילוד|תינוק|מתבגר", re.IGNORECASE)
INDIRECT = re.compile(DOMAINS["indirectness"], re.IGNORECASE)
YEAR = re.compile(r"\b(19|20)\d\d\b")


def mismatches(article):
    """[(part, what the paper studied)] where the agent marked no match."""
    fit = article.get("pico_fit") or {}
    return [(p, (fit.get(p) or {}).get("studied", "")) for p in PARTS
            if fit.get(p) and not fit[p].get("match", True)]


def pico_problems(articles, question, certainty, texts):
    """texts: {paper_id: title + study type + findings}. Returns problem strings for a send-back."""
    problems = []
    adult_question = not CHILDREN.search(question or "")
    for a in articles:
        fit = a.get("pico_fit") or {}
        pop = fit.get("population") or {}
        if adult_question and pop.get("match") and CHILDREN.search(texts.get(a["paper_id"], "")):
            problems.append(f"{a['paper_id']}: the paper mentions children but the question does not; mark "
                            "population as not matching (or partly, with what was studied)")
    if any(mismatches(a) for a in articles) and not INDIRECT.search(certainty or ""):
        problems.append("some papers do not match the question's PICO (see pico_fit): name this as "
                        "indirectness in `certainty`, and lower the rating or say why not")
    return problems


def trial_key(name):
    """'Climo et al. 2013' -> 'climo 2013'; 'CLEAN-IT' -> 'clean-it'."""
    words = re.findall(r"[^\W\d_][\w-]{2,}", name or "")
    word = next((w for w in words if w.lower() not in {"the", "trial", "study", "and", "et", "al"}), "")
    year = YEAR.search(name or "")
    return f"{word.lower()} {year.group()}".strip() if year else word.lower()


# characters: how close a trial's year must be printed after its first author's name. A reference list
# prints the year after the title ('Climo MW, Yokoe DS, ... Effect of daily chlorhexidine bathing ... N Engl J
# Med. 2013'): 150-190 characters in a real meta-analysis, so 120 missed it. Known limit: in a reference
# list, the next entry's year can fall inside the window.
NEAR = 250


def mentions(text, key):
    """Does `text` name this trial: the first author's surname with the year printed near it
    ('Climo et al. (2013)', 'Climo MW, ... 2013'), or the acronym when the key has no year."""
    word, _, year = key.partition(" ")
    if not word:
        return False
    low = (text or "").lower()
    for m in re.finditer(re.escape(word), low):
        if not year or year in low[max(0, m.start() - 20):m.end() + NEAR]:
            return True
    return False


def overlap(reviews, text_of):
    """reviews: [(label, paper_id, [trial names it lists])], every full-text review in the report, also
    those that listed nothing; text_of(paper_id) -> the review's full text.

    Every trial that some review lists (and whose name and year its own text contains) is looked for in
    EVERY review, by name and year: a review whose list the agent left empty still counts. A real report
    showed 'Climo 2013 in 2 of 4' although a third review discussed it.
    Returns (shared, dropped): shared = [(trial, [labels])] for trials in 2+ reviews, most shared first;
    dropped = [(label, name)] names that the listing review's own text does not contain."""
    texts = {pid: text_of(pid) or "" for _, pid, _ in reviews}
    trials, dropped = {}, []
    for label, pid, names in reviews:
        for name in names or []:
            key = trial_key(name)
            if mentions(texts[pid], key):
                trials.setdefault(key, name.strip())
            else:
                dropped.append((label, name))
    shared = []
    for key, shown in trials.items():
        labels = [label for label, pid, _ in reviews if mentions(texts[pid], key)]
        if len(labels) > 1:
            shared.append((shown, labels))
    return sorted(shared, key=lambda x: (-len(x[1]), x[0])), dropped
