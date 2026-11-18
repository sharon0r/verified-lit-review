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


def test_invented_author_name_is_caught_and_real_one_passes(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    arts = [article(pid, [])]
    assert agent._name_check(arts, "The review by Woodend and colleagues (2016) found 5 of 10.") == ["Woodend"]
    assert agent._name_check(arts, "Doe et al. found that 5 of 10 studies were effective.") == []
    assert agent._name_check(arts, "Doe ועמיתיו מצאו ירידה.") == []


def test_unread_important_paper_is_listed_with_link_and_no_results(setup):
    """A key paper that could not be downloaded is listed (title, link, why) - never its results."""
    store, pid, tmp = setup
    other = store.upsert_paper({"doi": "10.1/big", "title": "Postdischarge contacts: a meta-analysis",
                                "source": "test", "authors": "Boggan J, Roe K", "year": "2025"})
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    arts = [article(pid, [{"page": 1, "text": GOOD}])]
    sent_back = agent.submit_report(arts, not_read=[{"paper_id": other,
                                                     "why_important": "It found a 21% reduction."}])
    assert sent_back["accepted"] is False and sent_back["not_read_with_numbers"] == [other]
    ok = agent.submit_report(arts, not_read=[
        {"paper_id": other, "why_important": "The newest meta-analysis of randomized trials (2025)."},
        {"paper_id": "10.9/invented", "why_important": "Not from the searches."}])
    assert ok["accepted"] is True
    out = tmp / "r.md"
    agent._write_report(store.start_run("q", "m"), "q", "he", str(out), 1.0)
    md = out.read_text(encoding="utf-8")
    assert "מאמרים חשובים שלא ניתן היה לפתוח" in md
    assert "[Postdischarge contacts: a meta-analysis](https://doi.org/10.1/big) (Boggan et al., 2025)" in md
    assert "— **למה חשוב:** The newest meta-analysis of randomized trials (2025)." in md
    assert "invented" not in md and "21%" not in md


def test_send_back_is_fixed_with_small_edits_not_a_full_rewrite(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    bad = {**article(pid, [{"page": 1, "text": GOOD}, {"page": 1, "text": ALTERED}]),
           "bullets": ["Readmission fell from 18.2% to 9.9%."]}
    first = agent.submit_report([bad], conclusion="Mixed.", bottom_line="Calls may help.")
    assert first["accepted"] is False and "fix_report" in first["instruction"]
    ok = agent.fix_report([
        {"field": "quote", "paper_id": pid, "index": 1, "page": 0, "text": ""},            # remove bad quote
        {"field": "bullet", "paper_id": pid, "index": 0, "page": 0, "text": "Readmission fell to 18.2%."},
        {"field": "bottom_line", "paper_id": "", "index": 0, "page": 0, "text": "Calls may reduce readmission."}])
    assert ok["accepted"] is True
    assert agent.final[0]["quotes"] == [{"page": 1, "text": GOOD}]
    assert agent.bottom_line == "Calls may reduce readmission." and agent.conclusion == "Mixed."
    assert "error" in agent.fix_report([{"field": "bullet", "paper_id": "nope", "index": 0, "page": 0, "text": "x"}])


def test_too_long_conclusion_is_sent_back(setup):
    from verified_lit_review import agent as agent_mod
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    long = "word " * (agent_mod.CONCLUSION_MAX_WORDS + 1)
    r = agent.submit_report([article(pid, [{"page": 1, "text": GOOD}])], conclusion=long)
    assert r["accepted"] is False and "conclusion has" in r["too_long"][0]


def test_probably_with_low_certainty_is_sent_back(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    r = agent.submit_report([article(pid, [{"page": 1, "text": GOOD}])], certainty="נמוכה (משוער).",
                            bottom_line="רחצה כנראה מפחיתה זיהומים.")
    assert r["accepted"] is False and "עשויה להפחית" in r["too_long"][0]
    # moderate certainty may say "probably"; a low rating for a secondary outcome does not count
    assert Agent._certainty_verb_check("Moderate for BSI; low for VAP.", "probably reduces", "") == []


def test_reply_cut_off_at_the_output_limit_is_not_run_and_the_model_is_told(setup):
    store, pid, tmp = setup
    cut = reply(tool_call("submit_report", articles=[{"paper_id": pid}]), stop="max_tokens")
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)), cut,
                         reply(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}])]))])
    events = []
    r = Agent(store, str(tmp), client=client, on_event=lambda k, m: events.append(m)).run(
        "q", n_papers=1, out_path=str(tmp / "r.md"))
    told = client.requests[2]["messages"][-1]["content"][0]
    assert told["is_error"] and "cut off at the output limit" in told["content"]
    assert any("output limit" in m for m in events) and r["verified"] == 1
    assert client.requests[0]["max_tokens"] > 16000


def test_search_budget_ends_the_search_phase(setup, monkeypatch):
    """After MAX_SEARCH_TURNS turns with searches, search_papers refuses instead of searching again."""
    from verified_lit_review import agent as agent_mod
    store, pid, tmp = setup
    searched = []
    monkeypatch.setitem(agent_mod.SEARCHERS, "openalex", lambda q, s, n: (searched.append(q) or [], "test"))
    turns = [reply(tool_call("search_papers", query=f"q{i}", database="openalex", n=5))
             for i in range(agent_mod.MAX_SEARCH_TURNS + 1)]
    client = FakeClient(turns + [reply(tool_call("fetch_paper", paper_id=pid)),
                                 reply(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}])])),
                                 reply(NS(type="text", text="done"), stop="end_turn")])
    Agent(store, str(tmp), client=client).run("q", n_papers=1, out_path=str(tmp / "r.md"))
    assert len(searched) == agent_mod.MAX_SEARCH_TURNS
    refused = client.requests[agent_mod.MAX_SEARCH_TURNS + 1]["messages"][-1]["content"][0]
    assert refused["is_error"] and "search budget" in refused["content"]


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
    # 2 final quotes verified + the 2 quotes bounced at the first submission (kept in the audit trail)
    assert store.run_summary(r["run_id"]) == {"total": 4, "verified": 2, "rejected": 2}
    assert r["cost_usd"] == round(4000 / 1e6 * 4 + 800 / 1e6 * 20, 4)
    assert [k for k, _ in events if k != "timing"] == ["fetch", "retrieve", "verify", "verify"]
    assert r["timing"]["turns"] >= 3 and "model_seconds" in r["timing"]       # where the run time goes


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


def test_report_shows_database_route_and_why_chosen(setup):
    store, pid, tmp = setup
    store.conn.execute("UPDATE papers SET source='europepmc', pdf_route='PMC open-data (AWS)' WHERE paper_id=?", (pid,))
    store.conn.commit()
    art = {**article(pid, [{"page": 1, "text": GOOD}]), "why_chosen": "Largest randomized trial found."}
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[art], conclusion="Mixed evidence."))])
    Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert "**Why chosen:** Largest randomized trial found." in report
    assert "Found in" not in report and ".pdf" not in report        # no database, route or file name
    assert report.index("Mixed evidence.") < report.index("Papers in this review")   # conclusion first
    assert report.index("Papers in this review") < report.index("How this report was checked")


def test_paper_titles_are_clickable_links(setup):
    store, pid, tmp = setup
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}])]))])
    Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    report = (tmp / "r.md").read_text(encoding="utf-8")
    link = "[Calls after discharge](https://doi.org/10.1/tel)"
    assert "— [Doe, 2024](https://doi.org/10.1/tel)" in report and f"**Title:** {link}" in report  # list + section


def test_report_shows_pico_certainty_and_scope(setup):
    store, pid, tmp = setup
    art = article(pid, [{"page": 1, "text": GOOD}])
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[art], conclusion="Mixed.",
                                         pico="P: adults; I: calls; C: usual care; O: readmission",
                                         certainty="Low: non-randomized allocation; imprecision"))])
    Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert "**PICO:** P: adults; I: calls" in report
    assert "Certainty of evidence (GRADE-style, approximate):** Low" in report
    assert "not a systematic review" in report


def test_quotes_bounced_during_the_run_are_kept_in_the_audit_trail(setup):
    store, pid, tmp = setup
    client = FakeClient([
        reply(tool_call("fetch_paper", paper_id=pid)),
        reply(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}, {"page": 1, "text": ALTERED}])])),
        reply(tool_call("submit_report", articles=[article(pid, [{"page": 1, "text": GOOD}])])),
    ])
    r = Agent(store, str(tmp), client=client).run("q", n_papers=1, out_path=str(tmp / "r.md"))
    rows = store.conn.execute("SELECT verified, reason FROM quote_checks WHERE run_id=? ORDER BY rowid",
                              (r["run_id"],)).fetchall()
    assert [row["verified"] for row in rows] == [1, 0]
    assert "returned to the agent at submission 1" in rows[1]["reason"]


def test_conclusion_number_must_come_from_a_finding_not_just_anywhere_in_a_paper(setup):
    store, pid, tmp = setup
    art = {**article(pid, [{"page": 1, "text": GOOD}]), "study_type": "RCT"}
    # 18.2 is in a finding; 240 is printed in the paper (page 2) but no finding or quote carries it
    conclusion = "Readmission fell to 18.2% in one trial; another review pooled 240 trials."
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[art], conclusion=conclusion)),
                         reply(tool_call("submit_report", articles=[art], conclusion=conclusion))])
    r = Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    assert "240" in client.requests[2]["messages"][-1]["content"][0]["content"]   # returned to the agent
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert "checked against the verified findings: 1/2 found" in report
    assert "⚠️ **not in the findings:** 240" in report
    assert r["verified"] == 1


def test_invented_number_is_sent_back_and_flagged_if_kept(setup):
    store, pid, tmp = setup
    bad = {**article(pid, [{"page": 1, "text": GOOD}]), "bullets": ["Readmission fell from 18.2% to 9.9%."]}
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[bad])),
                         reply(tool_call("submit_report", articles=[bad]))])
    r = Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    feedback = client.requests[2]["messages"][-1]["content"][0]["content"]
    assert '"accepted": false' in feedback and "9.9" in feedback        # returned to the agent
    report = (tmp / "r.md").read_text(encoding="utf-8")
    # 18.2 (finding) and 240 (study type "n=240") are in the paper; 9.9 is not
    assert "⚠️ **failed the check:** 9.9 (not in the paper)" in report and "2/3 found" in report


def test_finding_number_on_another_page_than_cited_is_sent_back_with_the_reason(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    art = {**article(pid, [{"page": 1, "text": GOOD}]),
           "bullets": [{"text": "Readmission fell from 18.2% to 12.5%.", "pages": [2]}]}
    r = agent.submit_report([art])
    assert r["accepted"] is False
    assert r["number_warnings"][0]["numbers_not_in_paper"] == ["18.2", "12.5"]
    assert "not on the cited page" in r["number_warnings"][0]["problems"][0]
    # the agent fixes only the page of that bullet
    ok = agent.fix_report([{"field": "bullet", "paper_id": pid, "index": 0, "page": 1,
                            "text": "Readmission fell from 18.2% to 12.5%."}])
    assert ok["accepted"] is True and agent.final[0]["bullets"][0]["pages"] == [1]


def test_effect_verb_on_a_non_significant_result_is_sent_back(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    r = agent.submit_report([article(pid, [{"page": 1, "text": GOOD}])],
                            conclusion="Calls reduced readmission (RR 0.81, 95% CI 0.65 to 1.01).")
    assert r["accepted"] is False and "conclusion" in r["effect_claimed_on_non_significant_result"][0]
    ok = agent.fix_report([{"field": "conclusion", "paper_id": "", "index": 0, "page": 0,
                            "text": "Calls did not significantly reduce readmission."}])
    assert ok["accepted"] is True


def test_strong_study_that_could_not_be_opened_is_listed_and_certainty_must_say_provisional(setup):
    from verified_lit_review.agent import strength
    assert strength({"title": "Chlorhexidine bathing: a systematic review and meta-analysis"}) == "review"
    assert strength({"title": "Bathing of the critically ill", "doi": "10.1002/14651858.CD012248.pub2"}) == "review"
    assert strength({"title": "Daily bathing: a cluster-randomised crossover trial"}) == "trial"
    assert strength({"title": "Nurses' views on bathing"}) is None
    store, pid, tmp = setup
    cochrane = store.upsert_paper({"doi": "10.1002/14651858.CD012248.pub2", "source": "pubmed",
                                   "title": "Chlorhexidine bathing of the critically ill", "authors": "Lewis SR"})
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    agent.failed = [cochrane]                         # the agent tried it; no open PDF
    r = agent.submit_report([article(pid, [{"page": 1, "text": GOOD}])], certainty="Low.")
    assert r["accepted"] is False and "provisional" in r["certainty_must_be_provisional"]
    assert agent.not_read == [{"paper_id": cochrane, "why_important": "", "auto": "review"}]   # listed by the code
    ok = agent.fix_report([{"field": "certainty", "paper_id": "", "index": 0, "page": 0,
                            "text": "Low and provisional: the Cochrane review could not be opened."}])
    assert ok["accepted"] is True


def test_report_explains_an_automatically_listed_study_and_warns_when_most_strong_ones_are_missing(setup):
    store, pid, tmp = setup
    cochrane = store.upsert_paper({"doi": "10.1002/14651858.CD012248.pub2", "source": "pubmed",
                                   "title": "Chlorhexidine bathing of the critically ill", "authors": "Lewis SR",
                                   "year": "2019"})
    art = article(pid, [{"page": 1, "text": GOOD}])
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[art],
                                         certainty="Low, provisional: risk of bias and imprecision.",
                                         conclusion="", pico="", bottom_line="", not_read=[]))])
    agent = Agent(store, str(tmp), client=client)
    agent.failed = [cochrane]
    agent.run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert "A systematic review or meta-analysis the agent chose to read, but it was not open access." in report
    assert "Strong studies missing:** 1 of the 1 strongest" in report


def test_certainty_two_levels_down_with_one_reason_is_sent_back(setup):
    store, pid, tmp = setup
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    art = {**article(pid, [{"page": 1, "text": GOOD}]), "study_type": "randomized controlled trial, n=240"}
    r = agent.submit_report([art], certainty="Low: risk of bias.")
    assert r["accepted"] is False and "only 1 reason" in r["certainty_rating"] and agent.certainty_start == "high"
    ok = agent.fix_report([{"field": "certainty", "paper_id": "", "index": 0, "page": 0,
                            "text": "Low: risk of bias (no blinding) and imprecision (a single small trial)."}])
    assert ok["accepted"] is True


def test_one_trust_line_replaces_the_per_paper_check_lines_when_all_is_found(setup):
    store, pid, tmp = setup
    art = article(pid, [{"page": 1, "text": GOOD}])
    client = FakeClient([reply(tool_call("fetch_paper", paper_id=pid)),
                         reply(tool_call("submit_report", articles=[art], bottom_line="Calls may help.",
                                         conclusion="", pico="", certainty="", not_read=[]))])
    Agent(store, str(tmp), client=client).run("q", n_papers=1, language="en", out_path=str(tmp / "r.md"))
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert "✅ **Checked in code:** 1 quotes and 3 numbers were checked against the papers, and all were found." in report
    assert report.index("Checked in code") < report.index("## Papers in this review")
    assert "Numbers checked against the paper" not in report          # shown only when a number fails


ABSTRACT = ("Background: Daily bathing may prevent infection. Methods: cluster randomised crossover trial in "
            "22 ICUs. Results: The primary outcome occurred at 3.99 per 1,000 patient-days in the "
            "intervention arm vs 3.45 in the control arm. Conclusion: no reduction was found in this trial.")


def test_abstract_only_paper_is_marked_kept_out_of_conclusion_numbers_and_certainty(setup):
    store, pid, tmp = setup
    trial = store.upsert_paper({"doi": "10.1/clean-it", "source": "europepmc", "year": "2026",
                                "title": "Daily bathing: a cluster-randomised crossover trial",
                                "authors": "Tomazini BM", "journal": "Lancet Reg Health Am", "abstract": ABSTRACT})
    agent = Agent(store, str(tmp))
    agent.fetched.add(pid)
    assert "error" in agent.use_abstract(trial)                 # only after a failed download
    agent.failed = [trial]
    assert agent.use_abstract(trial)["pages"] == [0]
    quote = "The primary outcome occurred at 3.99 per 1,000 patient-days in the intervention arm vs 3.45 in the control arm."
    art2 = {"paper_id": trial, "topic": "t", "study_type": "cluster randomised trial", "relevance": "r",
            "bullets": [{"text": "3.99 per 1,000 patient-days in the intervention arm vs 3.45 in control.", "pages": [0]}],
            "quotes": [{"page": 0, "text": quote}]}
    art1 = {**article(pid, [{"page": 1, "text": GOOD}]), "study_type": "observational cohort, n=240"}
    r = agent.submit_report([art1, art2], conclusion="The trial found 3.99 vs 3.45.",
                            certainty="Low: observational evidence only.")
    assert r["accepted"] is False and "3.99" in r["number_warnings"][-1]["numbers_not_in_paper"]   # not in conclusion
    assert agent.certainty_start == "low"            # the abstract's randomized design does not raise the start
    ok = agent.fix_report([{"field": "conclusion", "paper_id": "", "index": 0, "page": 0,
                            "text": "One trial, read from its abstract only, found no reduction."}])
    assert ok["accepted"] is True
    out = agent._write_report(agent.store.start_run("q"), "q", "en", str(tmp / "r.md"), 1.0)
    report = (tmp / "r.md").read_text(encoding="utf-8")
    assert out["verified"] == 2 and "📄 **From the abstract only" in report and "— abstract · ✅ verified" in report
