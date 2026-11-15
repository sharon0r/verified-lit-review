# Evaluation

How do I know the reports are any good? Two kinds of evidence: what the code checks on every run, and
a review of the reports by AI reviewer agents, repeated after every change.

## 1. What the code checks on every run

| Check | How | Result in the runs below |
|---|---|---|
| Quotes | word for word on the cited PDF page (`build_report.norm`) | every final quote verified; one real quote bounced and fixed during a heart-failure run |
| Numbers | every number in findings and conclusion must appear in the paper (`numbers.py`) | 94 / 94 found in the latest heart-failure report |
| Sources | peer-reviewed, not retracted, open access (filters in `sources.py`) | enforced by query filters |

These checks prove that quotes and numbers *exist in the papers*. They do not prove that the paper
choice or the interpretation is right - that is what the review below is for.

## 2. Review by AI reviewer agents (LLM-as-judge)

**Method.** After each change, a report produced by the tool was given to a separate Claude agent,
instructed to review it as a physician-researcher (GRADE-experienced) or as a clinical psychologist
(meta-analysis-experienced): read-only, a 1-10 score, ranked problems, and whether they would rely on it
as a first-look review. The reviewers are **AI agents, not human experts.**

| Round | Change before this round | Question | Reviewer | Score |
|---|---|---|---|---|
| 1 | baseline (3 papers) | telephone follow-up after discharge | physician agent | 6 / 10 |
| 1 | + PICO, scope statement, GRADE-style certainty, "not significant is not no effect" | mindfulness for student anxiety | psychologist agent | 6 / 10 |
| 2 | + six reporting rules (GRADE verbs, overlap between reviews, effect-size metrics, "not reported is not none", ...), 5 papers | cardiac rehab in heart failure | physician agent | 7 / 10 |
| 2 | same | internet CBT for depression | psychologist agent | 7 / 10 |
| 3 | + PubMed search for recent systematic reviews (found Cochrane 2024) | cardiac rehab in heart failure | physician agent | 8.5 / 10 |
| 4 | + number guardrail, absolute effects, weighing by design, precise GRADE terms | cardiac rehab in heart failure | physician agent | 9 / 10 |

**What each round found, and what changed**

1. Misread statistics (an NNT interval "crossing zero"), conclusions stronger than the evidence, no
   statement of scope. -> Statistics rules, PICO, certainty rating, scope statement.
2. "Reduces" despite low certainty, overlapping reviews presented as independent confirmation, an SMD
   reported in "points", "no adverse events reported" read as "safe". -> Six reporting rules.
3. Both reviewers: the newest and broadest reviews (e.g. Cochrane 2024) were missing, so the conclusion
   was too pessimistic. -> PubMed search restricted to recent systematic reviews; the conclusion changed
   from "inconsistent" to "probably reduces" (RR 0.69, moderate certainty).
4. No absolute effect, misused GRADE terms, an observational cohort weighed like trials. -> Prompt rules
   and a code check on every number.

**Still open (round 4):** observational evidence placed next to trials when describing inconsistency;
no explicit certainty for long-term outcomes; no absolute difference per 100 patients. The last one is
a deliberate trade-off: the tool does not compute new numbers, so that every number can be checked
against the paper.

## 3. Which model: cost against quality

Same question (cardiac rehab in heart failure), same prompt and guardrails, same AI physician reviewer.

| Model | Papers | Cost per run | Quotes verified | Score | Rely on it as a first look? |
|---|---|---|---|---|---|
| Claude Opus 5.5 | 5 | ~$0.6 | 6 / 6 | 9 / 10 | yes |
| Claude Opus 5.5 | 4 | $0.43 | 5 / 5 | 7 / 10 | yes, with caution |
| Claude Haiku 5.5 | 7 | $0.03 | 11 / 11 | 6 / 10 | yes, with caution |
| Claude Sonnet 5.5 | 5 | $0.32 | 5 / 5 | 5 / 10 | no |

**What this shows.** The guardrails held for every model: no invented quote reached a report. What
the cheaper models lost was *judgement*: choosing the papers (the newest Cochrane review missing or
an outdated version used, overlapping reviews counted as independent, indirect cohorts) and weighing
them. The two Opus runs also differ (9 vs 7), so the choice of papers varies from run to run, and a
single run is a weak measurement. **Decision:** the app uses Opus only.

**A guardrail gap found and fixed.** The Sonnet reviewer noticed two numbers in the conclusion that came
from an abstract the agent never read, yet they were marked as checked: the check compared conclusion
numbers with the pooled text of all papers, where they appeared by chance. Now every number in the
conclusion must appear in a verified finding or quote, which are themselves checked against their own
paper (`agent._conclusion_check`, test `test_conclusion_number_must_come_from_a_finding...`).

**Prompt change from these reviews (not yet re-measured):** use the newest version of a review (e.g.
Cochrane pub6 over pub5), and include at least one primary study when choosing more than two papers.

## 4. Limitations of this evaluation

- **AI reviewers, not people.** A Claude agent judged reports written by a Claude agent; the same model
  family may share blind spots. A review by a human clinician is the next step.
- **Few runs.** One report per round; the agent is not deterministic, so another run of the same
  question can pick different papers.
- **Few fields.** End-to-end runs in medicine, psychology, education and the economics of work. Other
  fields were checked only for search coverage.
- The scores measure a rapid first-look review of up to five open-access papers, not a systematic review.
