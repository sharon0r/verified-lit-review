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
