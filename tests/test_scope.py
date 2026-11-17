"""Question check: a broad question gets focused options, a focused one goes straight on."""
from types import SimpleNamespace as NS

from verified_lit_review.scope import check_question


class FakeClient:
    def __init__(self, answer):
        self.answer, self.requests = answer, []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        return NS(content=[NS(type="tool_use", input=self.answer)],
                  usage=NS(input_tokens=1000, output_tokens=200))


def test_broad_question_returns_three_focused_options():
    c = FakeClient({"focused": False, "reason": "חסרים אוכלוסייה ותוצאה.",
                    "options": ["Does aerobic exercise reduce depression in older adults?",
                                "Does  exercise\nimprove sleep in adults with insomnia?",
                                "Does resistance training lower blood pressure?", "a fourth one"]})
    r = check_question("Is exercise good?", client=c)
    assert r["focused"] is False and len(r["options"]) == 3
    assert r["options"][1] == "Does exercise improve sleep in adults with insomnia?"
    assert c.requests[0]["tool_choice"]["name"] == "assess_question"
    assert "<question>Is exercise good?</question>" in c.requests[0]["messages"][0]["content"]
    assert 0 < r["cost_usd"] < 0.02


def test_focused_question_or_no_options_goes_straight_on():
    ok = check_question("q", client=FakeClient({"focused": True, "reason": "", "options": ["x"]}))
    assert ok["focused"] is True and ok["options"] == []
    empty = check_question("q", client=FakeClient({"focused": False, "reason": "", "options": []}))
    assert empty["focused"] is True              # nothing to choose from: do not block the review
