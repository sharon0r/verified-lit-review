"""Tests for the SQLite store and the polite Europe PMC client. No network access is used."""
import pytest
import requests

from verified_lit_review import sources
from verified_lit_review.db import Store

EPMC_JSON = {"resultList": {"result": [{
    "source": "MED", "id": "123", "title": "Telephone follow-up after <i>discharge</i>.",
    "authorString": "Doe J, Roe R.", "pubYear": "2023", "doi": "10.1000/xyz", "pmcid": "PMC999",
    "abstractText": "Calls reduced 30-day readmission.", "citedByCount": 7,
    "journalInfo": {"journal": {"title": "Test Journal"}},
    "fullTextUrlList": {"fullTextUrl": [
        {"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://example.org/a.pdf"},
        {"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://europepmc.org/a.pdf"},
        {"documentStyle": "html", "availabilityCode": "OA", "url": "https://example.org/a.html"}]}}]}}


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data


class FakeSession:
    """Plays back a scripted list of responses (or exceptions) and records the calls."""

    def __init__(self, script):
        self.script, self.calls, self.headers = list(script), [], {}

    def get(self, url, **kw):
        self.calls.append((url, kw))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake(monkeypatch):
    def install(script):
        session = FakeSession(script)
        monkeypatch.setattr(sources, "_client",
                            sources.PoliteClient(min_interval=0, retries=2, session=session))
        monkeypatch.setattr(sources.time, "sleep", lambda s: None)
        return session
    return install


# ------------------------------------------------------------------ store
def test_store_page_search_ranks_relevant_page_first(store):
    store.upsert_paper({"doi": "10.1/a", "title": "A", "source": "test"})
    store.add_pages("10.1/a", ["Methods: we enrolled patients.",
                               "Results: telephone follow-up reduced readmission by 20%.",
                               "Funding statement."])
    hits = store.search_pages("10.1/a", "Does telephone follow-up reduce readmission?")
    assert hits[0]["page"] == 2
    assert store.page_count("10.1/a") == 3


def test_store_audit_trail(store):
    run = store.start_run("q", model="test-model")
    store.log_quote(run, "10.1/a", 2, "real quote", True)
    store.log_quote(run, "10.1/a", 3, "fake quote", False, found_on=[], reason="not found")
    assert store.run_summary(run) == {"total": 2, "verified": 1, "rejected": 1}


# ------------------------------------------------------------------ Europe PMC
def test_query_is_built_from_content_words():
    q = sources.europepmc_query("Does telephone follow-up after discharge reduce readmission?")
    assert q == ('TITLE_ABS:"telephone" AND TITLE_ABS:"follow-up" AND TITLE_ABS:"discharge" '
                 'AND TITLE_ABS:"readmission" AND OPEN_ACCESS:y')


def test_parse_keeps_open_pdfs_and_skips_bot_protected_links():
    p = sources.parse_europepmc(EPMC_JSON)[0]
    assert p["title"] == "Telephone follow-up after discharge"
    assert p["pdf_urls"] == ["https://example.org/a.pdf"]
    assert p["doi"] == "10.1000/xyz"


def test_second_search_comes_from_cache(store, fake):
    session = fake([FakeResponse(data=EPMC_JSON)])
    first, s1 = sources.search_europepmc("telephone follow-up readmission", store)
    second, s2 = sources.search_europepmc("telephone follow-up readmission", store)
    assert (s1, s2) == ("network", "cache")
    assert first == second and len(session.calls) == 1
    assert store.get_paper("10.1000/xyz")["pmcid"] == "PMC999"


def test_retries_after_rate_limit_then_succeeds(store, fake):
    session = fake([FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(data=EPMC_JSON)])
    papers, status = sources.search_europepmc("telephone readmission", store)
    assert status == "network" and len(papers) == 1 and len(session.calls) == 2


def test_offline_without_cache_raises_clear_error(store, fake):
    fake([requests.ConnectionError()] * 3)
    with pytest.raises(sources.SourceError, match="nothing cached"):
        sources.search_europepmc("telephone readmission", store)


def test_offline_with_old_cache_falls_back_to_it(store, fake):
    key = sources.europepmc_query("telephone readmission") + "|n=10"
    store.put_cached_search("europepmc", key, [{"title": "old result"}])
    store.conn.execute("UPDATE search_cache SET created_at = 0")   # make it stale
    fake([requests.ConnectionError()] * 3)
    papers, status = sources.search_europepmc("telephone readmission", store)
    assert status == "cache (stale, offline)" and papers[0]["title"] == "old result"


OPENALEX_JSON = {"results": [{
    "id": "https://openalex.org/W123", "doi": "https://doi.org/10.5/edu", "display_name": "Formative feedback.",
    "publication_year": 2024, "cited_by_count": 3,
    "authorships": [{"author": {"display_name": "Ann Lee"}}, {"author": {"display_name": "Bo Kim"}}],
    "primary_location": {"source": {"display_name": "Journal of Education"}},
    "abstract_inverted_index": {"Feedback": [0], "raised": [1], "scores": [2]},
    "best_oa_location": {"pdf_url": "https://repo.example/a.pdf"},
    "locations": [{"pdf_url": "https://repo.example/a.pdf"}, {"pdf_url": "https://mirror.example/a.pdf"}],
    "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC777"}}]}


def test_parse_openalex_rebuilds_abstract_and_dedupes_pdfs():
    p = sources.parse_openalex(OPENALEX_JSON)[0]
    assert p["abstract"] == "Feedback raised scores"
    assert p["pdf_urls"] == ["https://repo.example/a.pdf", "https://mirror.example/a.pdf"]
    assert (p["doi"], p["pmcid"], p["authors"], p["year"]) == ("10.5/edu", "PMC777", "Ann Lee, Bo Kim", "2024")


def test_openalex_search_is_cached_and_filtered_to_open_access(store, fake):
    session = fake([FakeResponse(data=OPENALEX_JSON)])
    _, s1 = sources.search_openalex("Does formative feedback improve achievement?", store)
    _, s2 = sources.search_openalex("Does formative feedback improve achievement?", store)
    assert (s1, s2) == ("network", "cache") and len(session.calls) == 1
    assert session.calls[0][1]["params"]["filter"] == "open_access.is_oa:true"
    assert store.get_paper("10.5/edu")["title"] == "Formative feedback"


def test_user_agent_names_the_tool():
    c = sources.PoliteClient(session=FakeSession([]))
    assert c.s.headers["User-Agent"].startswith("verified-lit-review/")
