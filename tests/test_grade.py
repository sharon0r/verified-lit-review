"""Fourth guardrail: the GRADE start comes from the designs; every step down or up is explained."""
from verified_lit_review import grade


def test_designs_and_start_level():
    assert grade.design("Daily bathing: a cluster-randomised crossover trial") == "randomized"
    assert grade.design("ניסוי אשכולות במרכז יחיד, ללא סמיות") == "randomized"
    assert grade.design("Retrospective cohort of 2,000 ICU patients") == "observational"
    assert grade.design("סקירה של מחקרי עוקבה") == "observational"
    assert grade.design("Interviews with nurses") is None
    assert grade.start_level(["randomized", "observational"]) == "high"
    assert grade.start_level(["observational", None]) == "low"
    assert grade.start_level([None]) is None


def test_rated_level_in_english_and_hebrew():
    assert grade.rated_level("Low (approximate): risk of bias") == "low"
    assert grade.rated_level("נמוכה מאוד: מעט ניסויים") == "very low"
    assert grade.rated_level("בינונית לזיהומי דם; נמוכה ל-VAP") == "moderate"
    assert grade.rated_level("The evidence is weak") is None


def test_each_level_down_needs_a_named_reason():
    rct = ["randomized"]
    assert grade.check("Low: one level for risk of bias (no blinding), one for imprecision (wide CI).", rct)[1] is None
    assert grade.check("נמוכה: הורדה בשל סיכון להטיה (ללא סמיות) ובשל חוסר דיוק.", rct)[1] is None
    assert "only 1 reason" in grade.check("Low: risk of bias.", rct)[1]
    assert grade.check("Low: risk of bias was very serious (two levels).", rct)[1] is None
    assert "start `certainty`" in grade.check("The evidence is weak.", rct)[1]


def test_rating_above_the_start_needs_an_upgrade_reason():
    obs = ["observational"]
    assert "upgrading" in grade.check("Moderate: consistent cohorts.", obs)[1]
    assert grade.check("Moderate: upgraded for a large effect (RR 0.2).", obs)[1] is None
    assert grade.check("Low: observational studies only.", obs)[1] is None
    assert grade.check("Moderate.", [None])[1] is None            # designs not recognised: not checked
