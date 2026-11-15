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
                 'AND TITLE_ABS:"readmission" AND ' + sources.EPMC_FILTER)
    assert "NOT SRC:PPR" in q and "retracted" in q          # no preprints, no retracted papers


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
    f = session.calls[0][1]["params"]["filter"]
    for required in ("open_access.is_oa:true", "type:article|review", "source.type:journal",
                     "is_retracted:false", "is_core:true"):
        assert required in f
    assert store.get_paper("10.5/edu")["title"] == "Formative feedback"


def test_user_agent_names_the_tool():
    c = sources.PoliteClient(session=FakeSession([]))
    assert c.s.headers["User-Agent"].startswith("verified-lit-review/")


def test_empty_search_result_is_not_cached(store, fake):
    session = fake([FakeResponse(data={"results": []}), FakeResponse(data=OPENALEX_JSON)])
    first, _ = sources.search_openalex("formative feedback", store)
    second, status = sources.search_openalex("formative feedback", store)
    assert first == [] and status == "network" and len(second) == 1 and len(session.calls) == 2


def test_upsert_keeps_pmcid_and_pdf_links_from_an_earlier_source(store):
    store.upsert_paper({"doi": "10.1/k", "pmcid": "PMC5", "title": "T", "source": "europepmc",
                        "pdf_urls": ["https://x.example/a.pdf"]})
    store.upsert_paper({"doi": "10.1/k", "pmcid": None, "title": "T", "source": "openalex", "pdf_urls": []})
    p = store.get_paper("10.1/k")
    assert p["pmcid"] == "PMC5" and p["pdf_urls"] == ["https://x.example/a.pdf"]


PUBMED_XML = """<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>
<Journal><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue><Title>Cochrane Database Syst Rev</Title></Journal>
<ArticleTitle>Exercise-based cardiac rehabilitation for adults with <i>heart failure</i>.</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Heart failure is common.</AbstractText><AbstractText>Exercise reduced admissions.</AbstractText></Abstract>
<AuthorList><Author><LastName>Molloy</LastName><Initials>CD</Initials></Author><Author><LastName>Long</LastName><Initials>L</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Systematic Review</PublicationType><PublicationType>Meta-Analysis</PublicationType></PublicationTypeList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">38323679</ArticleId>
<ArticleId IdType="doi">10.1002/14651858.CD003331.pub6</ArticleId><ArticleId IdType="pmc">PMC10849021</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""


class TextResponse(FakeResponse):
    def __init__(self, text):
        super().__init__(data=None)
        self.text = text


def test_pubmed_term_asks_for_recent_open_reviews_only():
    t = sources.pubmed_term("Does exercise reduce readmissions in heart failure?")
    for part in ("systematic review[pt]", "meta-analysis[pt]", "free full text[sb]", "NOT retracted publication[pt]", ":3000[dp]"):
        assert part in t


def test_parse_pubmed_xml():
    p = sources.parse_pubmed_xml(PUBMED_XML)[0]
    assert p["title"] == "Exercise-based cardiac rehabilitation for adults with heart failure"
    assert (p["doi"], p["pmcid"], p["year"], p["authors"]) == (
        "10.1002/14651858.CD003331.pub6", "PMC10849021", "2024", "Molloy CD, Long L")
    assert p["abstract"] == "Heart failure is common. Exercise reduced admissions."
    assert "Systematic Review" in p["pub_types"]


def test_pubmed_search_two_calls_then_cache(store, fake):
    session = fake([FakeResponse(data={"esearchresult": {"idlist": ["38323679"]}}), TextResponse(PUBMED_XML)])
    papers, s1 = sources.search_pubmed_reviews("exercise heart failure readmission", store)
    _, s2 = sources.search_pubmed_reviews("exercise heart failure readmission", store)
    assert (s1, s2) == ("network", "cache") and len(session.calls) == 2
    assert session.calls[0][1]["params"]["tool"] == "verified-lit-review"
    assert store.get_paper("10.1002/14651858.CD003331.pub6")["pmcid"] == "PMC10849021"
