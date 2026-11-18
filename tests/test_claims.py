"""Third guardrail: words that claim an effect must fit the confidence interval."""
from verified_lit_review import claims


def test_estimates_are_read_in_english_and_hebrew_formats():
    e = claims.estimates("readmission RR 0.81 (95% CI 0.65 to 1.01); mortality HR 0.70 (0.55-0.89)")
    assert [(x["x"], x["a"], x["b"], x["null"]) for x in e] == [(0.81, 0.65, 1.01, True), (0.70, 0.55, 0.89, False)]
    he = claims.estimates("(יחס שיעורים 0.60, רווח סמך 95% 0.34 עד 1.04)")
    assert he[0]["kind"] == "ratio" and he[0]["null"] is True
    md = claims.estimates("mean difference -0.48 days (95% CI -0.90 to -0.06)")
    assert md[0]["kind"] == "difference" and md[0]["null"] is False
    assert claims.estimates("in 12 trials (3-5 per country)") == []        # no label: not judged


def test_plain_effect_verb_on_a_non_significant_estimate_is_flagged():
    assert claims.overclaims("Calls reduced readmission (RR 0.81, 95% CI 0.65 to 1.01).")
    assert claims.overclaims("רחצה כנראה מפחיתה זיהומים (יחס שיעורים 0.60, רווח סמך 95% 0.34 עד 1.04).")
    assert claims.overclaims("The intervention lowered length of stay (mean difference -0.3, 95% CI -0.8 to 0.2).")


def test_negated_hedged_or_significant_wording_is_allowed():
    assert not claims.overclaims("Calls did not significantly reduce readmission (RR 0.81, 95% CI 0.65 to 1.01).")
    assert not claims.overclaims("Calls may reduce readmission (RR 0.81, 95% CI 0.65 to 1.01).")
    assert not claims.overclaims("רחצה לא הפחיתה באופן מובהק (יחס סיכון 0.81, רווח סמך 95% 0.65 עד 1.01).")
    assert not claims.overclaims("Bathing reduced bloodstream infection (IRR 0.71, 95% CI 0.51 to 0.98).")
    # the verb belongs to the significant result; the other clause only reports a number
    assert not claims.overclaims("Bathing reduced BSI (IRR 0.71, 0.51-0.98), but the CLABSI estimate was "
                                 "imprecise (IRR 0.60, 0.34-1.04).")
    assert not claims.overclaims("ההשפעה על CLABSI גדולה יותר אך פחות מדויקת (יחס שיעורים 0.60, "
                                 "רווח סמך 95% 0.34 עד 1.04).")


def test_opposite_significant_directions_warn_unless_the_conclusion_says_so():
    papers = {"a": "RR 0.70 (0.55 to 0.89)", "b": "RR 1.40 (1.10 to 1.80)"}
    assert claims.direction_warnings(papers, "Bathing reduces infection.")
    assert not claims.direction_warnings(papers, "Results were conflicting between the trials.")
    assert not claims.direction_warnings(papers, "התוצאות סותרות בין המחקרים.")
    assert not claims.direction_warnings({"a": "RR 0.70 (0.55 to 0.89)", "b": "RR 0.90 (0.70 to 1.10)"}, "x")


def test_between_wording_and_effect_sizes_are_read():
    e = claims.estimates("ES=0.19 (רווח בר-סמך 95% בין 0.15 ל-0.23)")
    assert e[0]["kind"] == "difference" and (e[0]["a"], e[0]["b"]) == (0.15, 0.23) and e[0]["null"] is False
    assert claims.overclaims("Feedback improved scores (SMD 0.10, 95% CI between -0.05 and 0.25).")


def test_compared_values_must_name_their_arms():
    # the real CLEAN-IT finding: which arm had 3.99?
    assert claims.unnamed_arms("3⋅99 לעומת 3⋅45 ל-1,000 ימי אשפוז (יחס שיעורים 1⋅09)")
    assert not claims.unnamed_arms("3.99 בזרוע הכלורהקסידין לעומת 3.45 בזרוע הביקורת")
    assert not claims.unnamed_arms("2.25 vs 5.01 per 1,000 days in the intervention and control ICUs")
    assert not claims.unnamed_arms("23.2 בכלורהקסידין לעומת 40.9 בסבון", ["כלורהקסידין", "סבון"])


def test_secondary_outcome_must_be_marked_exploratory():
    assert claims.unlabelled_secondary("Secondary outcome: fewer resistant organisms (p=0.03).")
    assert not claims.unlabelled_secondary("Secondary outcome (exploratory): fewer resistant organisms.")
    assert not claims.unlabelled_secondary("תוצאה משנית, ממצא גישוש: פחות חיידקים עמידים.")
    assert not claims.unlabelled_secondary("Primary outcome: no difference.")
