"""The number guardrail: numbers in AI-written findings must appear in the paper."""
from verified_lit_review import numbers

PAGES = ["Readmission fell from 18.2% to 12.5% (RR 0.69, 95% CI 0.56 to 0.86) among 1,602 students.",
         "Adjusted IRR 0.719 (0.617-0.837). Published 2024. Of 10 studies, 5 were effective."]


def test_extract_skips_small_integers_and_years_and_normalizes_thousands():
    assert numbers.extract("In 2024, 5 of 10 trials (n = 1,602) gave RR 0.69 and 33.8%.") == [
        "10", "1602", "0.69", "33.8"]


def test_numbers_present_in_the_paper_are_verified():
    ok, bad = numbers.check("RR 0.69 (0.56-0.86); readmission 18.2% vs 12.5%; 1602 students",
                            numbers.PaperNumbers(PAGES))
    assert bad == [] and set(ok) == {"0.69", "0.56", "0.86", "18.2", "12.5", "1602"}


def test_rounded_value_counts_as_found():
    ok, bad = numbers.check("adjusted IRR 0.72 (0.62 to 0.84)", numbers.PaperNumbers(PAGES))
    assert bad == [] and ok == ["0.72", "0.62", "0.84"]


def test_invented_or_computed_numbers_are_flagged():
    ok, bad = numbers.check("RR 0.59; about 79 fewer per 1000; NNT 13", numbers.PaperNumbers(PAGES))
    assert bad == ["0.59", "79", "1000", "13"]


def test_decimal_comma_in_the_paper_matches_a_decimal_point_in_the_report():
    # a Spanish review printed "23,2 vs 40,9 (p=0,034)"; the report kept the commas in Hebrew text
    paper = numbers.PaperNumbers(["Infecciones 23,2 frente a 40,9 por 1.000 días (p=0,034); n=1,000"])
    ok, bad = numbers.check("23.2 vs 40.9 per 1,000 patient-days (p=0.034)", paper)
    assert bad == [] and set(ok) == {"23.2", "40.9", "1000", "0.034"}


def test_long_author_list_is_marked_as_shortened():
    from verified_lit_review.sources import author_list
    assert author_list([f"A{i}" for i in range(11)]) == "A0, A1, A2, A3, A4, A5, A6, A7, et al."
    assert author_list(["Doe J", "Roe R"]) == "Doe J, Roe R"


def test_text_without_numbers_has_nothing_to_check():
    assert numbers.check("Participants described feeling more connected.", numbers.PaperNumbers(PAGES)) == ([], [])


# ------------------------------------------------------------------ page-level checks of a finding
def idx(**pages):
    return numbers.PageIndex({int(k[1:]): v for k, v in pages.items()})


def test_number_must_be_on_the_cited_page():
    index = idx(p1="Primary outcome: RR 0.75 (95% CI 0.60 to 0.94).", p2="Mortality was 12.4% in both arms.")
    assert numbers.check_finding("RR 0.75 (0.60-0.94)", [1], index)[1] == []
    ok, bad, problems = numbers.check_finding("RR 0.75 (0.60-0.94)", [2], index)
    assert bad == ["0.75", "0.60", "0.94"] and problems[0][1] == "not_on_page" and "[1]" in problems[0][2]
    assert numbers.check_finding("RR 0.81", [1], index)[2][0][1] == "not_in_paper"


def test_minus_sign_must_match_and_a_range_dash_is_not_a_minus():
    neg = idx(p1="Length of stay: mean difference -0.48 days (-0.90 to -0.06).")
    pos = idx(p1="Length of stay fell by 0.48 days (95% CI 0.06 to 0.90).")
    assert numbers.check_finding("mean difference -0.48 days (-0.90 to -0.06)", [1], neg)[1] == []
    assert numbers.check_finding("a reduction of 0.48 days", [1], neg)[1] == []   # positive wording is fine
    ok, bad, problems = numbers.check_finding("mean difference -0.48 days", [1], pos)
    assert bad == ["0.48"] and problems[0][1] == "sign"
    # "0.60-0.94" is a range, "COVID-19" a name: neither is a negative number
    assert numbers.check_finding("RR 0.75 (0.60-0.94)", [1], idx(p1="RR 0.75 (0.60-0.94)"))[1] == []
    assert not numbers._negative("COVID-19", 6) and not numbers._negative("0.60-0.94", 5)


def test_estimate_and_interval_must_be_printed_together():
    apart = idx(p1="Primary: RR 0.75 for readmission. " + "x " * 200 + "Other limits 0.60 and 0.94 elsewhere.")
    ok, bad, problems = numbers.check_finding("RR 0.75 (95% CI 0.60 to 0.94)", [1], apart)
    assert bad == ["0.75"] and problems[0][1] == "ci_apart"
    he = idx(p1="Odds ratio 0.74 (95% CI 0.60-0.90), I2 = 36%.")
    assert numbers.check_finding("יחס סיכויים 0.74 (רווח סמך 95% 0.60 עד 0.90)", [1], he)[1] == []


def test_finding_without_pages_is_checked_against_the_whole_paper():
    index = idx(p1="n = 240", p2="RR 0.75 (0.60-0.94)")
    assert numbers.check_finding("RR 0.75 (0.60-0.94) in 240 patients", [], index)[1] == []


def test_deliberate_errors_are_caught():
    """The other side of 'no false alarms': broken versions of a correct finding must fail."""
    index = idx(p1="Readmission: RR 0.75 (95% CI 0.60 to 0.94); mortality RR 0.85 (0.70 to 1.02).",
                p2="Baseline characteristics were similar in both arms.")
    assert numbers.check_finding("RR 0.75 (0.60 to 0.94)", [1], index)[1] == []          # correct
    assert numbers.check_finding("RR -0.75 (0.60 to 0.94)", [1], index)[2][0][1] == "sign"
    assert numbers.check_finding("RR 0.75 (0.60 to 0.94)", [2], index)[2][0][1] == "not_on_page"
    assert numbers.check_finding("RR 0.76 (0.60 to 0.94)", [1], index)[2][0][1] == "not_in_paper"
    # a real estimate paired with another result's interval: every number exists, the pairing is wrong
    assert numbers.check_finding("RR 0.85 (0.60 to 0.94)", [1], index)[2][0][1] == "ci_apart"
