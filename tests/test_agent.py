"""Agent loop tests with a scripted fake model: no API key, no network, no cost."""
from types import SimpleNamespace as NS

import pymupdf
import pytest

from verified_lit_review.agent import Agent
from verified_lit_review.db import Store

P1 = "Results. Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 12.5% (p<0.01)."
P2 = "Limitations. The trial was conducted in a single academic centre with a small sample of 240 patients."
GOOD = "Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 12.5% (p<0.01)."
WRONG_PAGE = "The trial was conducted in a single academic centre with a small sample of 240 patients."
ALTERED = "Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 10.5% (p<0.01)."


def tool_call(name, **args):
    return NS(type="tool_use", id=f"call_{name}_{len(args)}", name=name, input=args)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=1000, output_tokens=200))


class FakeClient:
    """Plays a scripted conversation and records what the agent sent back."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})  # snapshot: the list keeps growing
        return self.script.pop(0)


@pytest.fixture
def setup(tmp_path):
    doc = pymupdf.open()
    for text in (P1, P2):
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 540, 760), text, fontsize=10)
    pdf = tmp_path / "paper.pdf"
    doc.save(pdf)
    store = Store(str(tmp_path / "t.db"))
    pid = store.upsert_paper({"doi": "10.1/tel", "title": "Calls after discharge", "source": "test",
                              "authors": "Doe J", "year": "2024", "journal": "J Test"})
    with pymupdf.open(pdf) as d:
        store.add_pages(pid, [p.get_text() for p in d])
    store.set_pdf(pid, str(pdf))
    yield store, pid, tmp_path
    store.close()


def article(pid, quotes):
    return {"paper_id": pid, "topic": "readmission", "study_type": "RCT, n=240",
            "bullets": ["Readmission fell from 18.2% to 12.5%."], "relevance": "Direct answer; single centre.",
            "quotes": quotes}


def test_agent_fixes_rejected_quotes_and_report_keeps_only_verified(setup):
    store, pid, tmp = setup
    client = FakeClient([
        reply(tool_call("fetch_paper", paper_id=pid)),
        reply(tool_call("retrieve_passages", paper_id=pid, query="readmission results", k=2)),
        reply(tool_call("submit_report", articles=[article(pid, [
            {"page": 1, "text": GOOD}, {"page": 1, "text": WRONG_PAGE}, {"page": 1, "text": ALTERED}])])),
        reply(tool_call("submit_report", articles=[article(pid, [
            {"page": 1, "text": GOOD}, {"page": 2, "text": WRONG_PAGE}])])),
    ])
    events = []
    agent = Agent(store, str(tmp / "pdfs"), client=client, on_event=lambda k, m: events.append((k, m)))
    r = agent.run("Does telephone follow-up reduce readmissions?", n_papers=1, language="en",
                  out_path=str(tmp / "report.md"))

    # the first submission was bounced back with the real page of the misattributed quote
    feedback = client.requests[3]["messages"][-1]["content"][0]["content"]
    assert '"accepted": false' in feedback and "found on [2]" in feedback and "not found" in feedback

    report = (tmp / "report.md").read_text(encoding="utf-8")
    assert GOOD in report and WRONG_PAGE in report and ALTERED not in report
    assert r["verified"] == 2 and r["rejected"] == []
    assert store.run_summary(r["run_id"]) == {"total": 2, "verified": 2, "rejected": 0}
    assert r["cost_usd"] == round(4000 / 1e6 * 4 + 800 / 1e6 * 20, 4)
    assert [k for k, _ in events] == ["fetch", "retrieve", "verify", "verify"]


def test_after_last_allowed_submission_bad_quotes_are_dropped_not_trusted(setup):
    store, pid, tmp = setup
    bad = [{"page": 1, "text": ALTERED}, {"page": 1, "text": GOOD}]
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[article(pid, bad)])),
                         reply(tool_call("submit_report", articles=[article(pid, bad)]))])
    r = Agent(store, str(tmp), client=client).run("q", n_papers=1, out_path=str(tmp / "r.md"))
    assert r["verified"] == 1 and len(r["rejected"]) == 1
    assert store.run_summary(r["run_id"])["rejected"] == 1
    assert ALTERED not in (tmp / "r.md").read_text(encoding="utf-8")


def test_reading_before_fetching_is_refused(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp), client=FakeClient([]))
    assert agent.run_tool("retrieve_passages", {"paper_id": pid, "query": "x", "k": 1}) == {"error": "fetch_paper first"}
    assert "error" in agent.run_tool("delete_everything", {})


def test_agent_that_never_submits_raises(setup):
    store, pid, tmp = setup
    client = FakeClient([reply(NS(type="text", text="I could not find papers."), stop="end_turn")])
    with pytest.raises(RuntimeError, match="without submitting"):
        Agent(store, str(tmp), client=client).run("q", out_path=str(tmp / "r.md"))


def test_submitting_a_paper_that_was_never_fetched_is_refused(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp), client=FakeClient([]))
    out = agent.run_tool("submit_report", {"articles": [article(pid, [{"page": 1, "text": GOOD}])]})
    assert "never fetched" in out["error"]
    assert "at least one" in agent.run_tool("submit_report", {"articles": []})["error"]


def test_cost_limit_stops_a_runaway_run_and_closes_it(setup, monkeypatch):
    store, pid, tmp = setup
    import verified_lit_review.agent as agent_mod
    monkeypatch.setattr(agent_mod, "MAX_COST_USD", 0.001)
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid))])
    with pytest.raises(RuntimeError, match="cost limit"):
        Agent(store, str(tmp), client=client).run("q", out_path=str(tmp / "r.md"))
    run = store.conn.execute("SELECT finished_at, error, cost_usd FROM runs").fetchone()
    assert run["finished_at"] and "cost limit" in run["error"] and run["cost_usd"] > 0


def test_unexpected_tool_error_is_returned_to_the_model_not_raised(setup, monkeypatch):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp), client=FakeClient([]))
    monkeypatch.setattr(agent, "fetch_paper", lambda paper_id: 1 / 0)
    assert "ZeroDivisionError" in agent.run_tool("fetch_paper", {"paper_id": pid})["error"]


def test_served_model_and_cost_are_recorded(setup):
    store, pid, tmp = setup
    resp = lambda *b: NS(content=list(b), stop_reason="tool_use", model="claude-opus-5-5",
                         usage=NS(input_tokens=1000, output_tokens=200))
    client = FakeClient([resp(tool_call("fetch_paper", paper_id=pid)),
                         resp(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}])]))])
    r = Agent(store, str(tmp), client=client).run("q", n_papers=1, out_path=str(tmp / "r.md"))
    row = store.conn.execute("SELECT served_model, cost_usd FROM runs WHERE run_id=?", (r["run_id"],)).fetchone()
    assert row["served_model"] == "claude-opus-5-5" and row["cost_usd"] == r["cost_usd"]
