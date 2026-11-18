"""PICO fit to the question, and trials shared between reviews."""
from verified_lit_review import fit

ALL_MATCH = {p: {"match": True, "studied": ""} for p in fit.PARTS}


def art(pid, **pico):
    return {"paper_id": pid, "pico_fit": {**ALL_MATCH, **pico}}


def test_children_cannot_match_an_adult_question():
    a = art("p1")
    problems = fit.pico_problems([a], "Does bathing reduce infections in ICU patients?", "Low: imprecision.",
                                 {"p1": "4 randomised trials, one in a paediatric ICU"})
    assert problems and "children" in problems[0]
    # a question about children, or a paper without children, passes
    assert not fit.pico_problems([a], "bathing in paediatric ICUs", "Low.", {"p1": "paediatric ICU"})
    assert not fit.pico_problems([a], "bathing in ICU patients", "Low.", {"p1": "adult ICU trial"})


def test_a_mismatch_must_be_named_as_indirectness_in_the_certainty():
    a = art("p1", population={"match": False, "studied": "adults and children"})
    assert fit.mismatches(a) == [("population", "adults and children")]
    assert fit.pico_problems([a], "q", "Low: risk of bias.", {"p1": ""})
    assert not fit.pico_problems([a], "q", "Low: risk of bias and indirectness (mixed ages).", {"p1": ""})
    assert not fit.pico_problems([a], "q", "נמוכה: סיכון להטיה ועקיפות.", {"p1": ""})


def test_trials_shared_by_reviews_are_reported_and_unknown_names_dropped():
    texts = {"r1": "Included trials: Climo et al. (2013), Noto 2015, Milstone 2013.",
             "r2": "We pooled Climo 2013 and Noto 2015.",
             "r3": "Trials: Climo 2013; Boonyasiri 2016."}
    reviews = [("Afonso 2016", "r1", ["Climo 2013", "Noto 2015", "Milstone 2013"]),
               ("Frost 2018", "r2", ["Climo 2013", "Noto 2015", "Invented 2020"]),
               ("Esarte 2022", "r3", ["Climo et al. 2013", "Boonyasiri 2016"])]
    shared, dropped = fit.overlap(reviews, texts.get)
    assert shared == [("Climo 2013", ["Afonso 2016", "Frost 2018", "Esarte 2022"]),
                      ("Noto 2015", ["Afonso 2016", "Frost 2018"])]
    assert dropped == [("Frost 2018", "Invented 2020")]
    assert fit.trial_key("CLEAN-IT") == "clean-it" and fit.trial_key("Climo et al., 2013") == "climo 2013"
