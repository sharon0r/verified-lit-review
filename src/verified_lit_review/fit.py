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


def overlap(reviews, text_of):
    """reviews: [(label, paper_id, [trial names])]; text_of(paper_id) -> the paper's full text.
    Returns (shared, dropped): shared = [(trial, [labels])] for trials in 2+ reviews, most shared first;
    dropped = [(label, name)] names that the review's own text does not contain."""
    seen, shown, dropped = {}, {}, []
    for label, pid, names in reviews:
        text = (text_of(pid) or "").lower()
        for name in names or []:
            key = trial_key(name)
            word = key.split(" ")[0]
            if not word or word not in text:
                dropped.append((label, name))
                continue
            if label not in seen.setdefault(key, []):
                seen[key].append(label)
                shown.setdefault(key, name.strip())
    shared = sorted(((shown[k], labels) for k, labels in seen.items() if len(labels) > 1),
                    key=lambda x: (-len(x[1]), x[0]))
    return shared, dropped
