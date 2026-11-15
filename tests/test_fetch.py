"""Tests for fetch.py: route order, PDF validation, page indexing and the cache. No network."""
import pymupdf
import pytest

from verified_lit_review import fetch, sources
from verified_lit_review.db import Store

LONG = "Telephone follow-up after discharge reduced thirty-day readmission among older adults. " * 4


def make_pdf(pages):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 540, 760), text, fontsize=10)
    return doc.tobytes()


PDF = make_pdf([LONG, "Discussion page. " + LONG])
S3_LIST = ("<ListBucketResult><Key>PMC1.1/PMC1.1.pdf</Key><Key>PMC1.1/supp1.pdf</Key>"
           "<Key>PMC1.2/PMC1.2.pdf</Key></ListBucketResult>")


class Resp:
    def __init__(self, status=200, content=b"", text="", data=None):
        self.status_code, self.content, self.text, self._data, self.headers = status, content, text, data, {}

    def json(self):
        return self._data


class Router:
    """Fake session: answers by URL prefix and records every URL requested."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, **kw):
        self.calls.append(url)
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        return Resp(404)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def route(monkeypatch):
    def install(routes):
        router = Router(routes)
        monkeypatch.setattr(sources, "_client", sources.PoliteClient(min_interval=0, retries=0, session=router))
        return router
    return install


PAPER = {"source": "europepmc", "source_id": "MED:1", "title": "Calls after discharge", "doi": "10.1/x",
         "pmcid": "PMC1", "pdf_urls": ["https://publisher.example/x.pdf"]}


def test_pmc_s3_picks_latest_article_pdf_not_supplements(route):
    route({fetch.PMC_S3: Resp(text=S3_LIST)})
    assert fetch.pmc_s3_url("PMC1") == fetch.PMC_S3 + "PMC1.2/PMC1.2.pdf"


def test_first_working_route_wins_and_pages_are_indexed(store, route, tmp_path):
    router = route({fetch.PMC_S3 + "PMC1.2": Resp(content=PDF), fetch.PMC_S3: Resp(text=S3_LIST)})
    r = fetch.fetch_paper(PAPER, store, tmp_path)
    assert r["status"] == "ok" and r["route"] == "PMC open-data (AWS)" and r["pages"] == 2
    assert not any("publisher.example" in u or "openalex" in u for u in router.calls)  # lazy routes
    assert store.search_pages("10.1/x", "discussion readmission")[0]["page"] == 2
    assert len(store.get_paper("10.1/x")["pdf_sha256"]) == 64


def test_html_landing_page_is_rejected_and_next_route_tried(store, route, tmp_path):
    route({"https://publisher.example": Resp(content=b"<html>Access denied</html>"),
           fetch.OPENALEX: Resp(data={"best_oa_location": {"pdf_url": "https://repo.example/x.pdf"}}),
           "https://repo.example": Resp(content=PDF)})
    r = fetch.fetch_paper({**PAPER, "pmcid": None}, store, tmp_path)
    assert r["status"] == "ok" and r["route"] == "OpenAlex"
    assert "not a PDF" in r["attempts"][0]["result"]


def test_no_open_route_fails_cleanly(store, route, tmp_path):
    route({})
    r = fetch.fetch_paper(PAPER, store, tmp_path)
    assert r["status"] == "failed" and not list(tmp_path.iterdir())


def test_scanned_pdf_without_text_is_skipped(store, route, tmp_path):
    route({"https://publisher.example": Resp(content=make_pdf([""]))})
    r = fetch.fetch_paper({**PAPER, "pmcid": None, "doi": None}, store, tmp_path)
    assert r["status"] == "failed" and "no text layer" in r["attempts"][0]["result"]


def test_second_fetch_uses_cache_without_network(store, route, tmp_path):
    route({"https://publisher.example": Resp(content=PDF)})
    fetch.fetch_paper({**PAPER, "pmcid": None}, store, tmp_path)
    router = route({})
    r = fetch.fetch_paper({**PAPER, "pmcid": None}, store, tmp_path)
    assert r["status"] == "cached" and router.calls == []


def test_fetch_papers_stops_after_wanted_count(store, route, tmp_path):
    route({"https://publisher.example": Resp(content=PDF)})
    papers = [{**PAPER, "pmcid": None, "doi": f"10.1/{i}", "pdf_urls": [f"https://publisher.example/{i}.pdf"]}
              for i in range(5)]
    results = fetch.fetch_papers(papers, store, tmp_path, want=2)
    assert len(results) == 2 and all(r["status"] == "ok" for r in results)


def test_safe_name_is_unique_for_similar_ids():
    assert fetch.safe_name("10.1000/a.b") != fetch.safe_name("10.1000/ab")


def test_pmc_lookup_failure_does_not_stop_other_routes(store, route, tmp_path, monkeypatch):
    route({"https://publisher.example": Resp(content=PDF)})
    def broken(_pmcid):
        raise sources.SourceError("S3 down")
    monkeypatch.setattr(fetch, "pmc_s3_url", broken)
    r = fetch.fetch_paper(PAPER, store, tmp_path)
    assert r["status"] == "ok" and r["route"] == "search result link"


@pytest.mark.parametrize("url", ["http://127.0.0.1/a.pdf", "http://169.254.169.254/latest/meta-data",
                                 "file:///etc/passwd", "ftp://example.org/a.pdf", "http://10.0.0.5/x.pdf"])
def test_private_or_non_http_urls_are_refused(url, tmp_path):
    ok, msg = fetch.download_pdf(url, str(tmp_path / "x.pdf"))
    assert not ok and msg.startswith("refused")


def test_oversized_pdf_is_refused_by_content_length(store, route, tmp_path):
    big = Resp(content=PDF)
    big.headers = {"Content-Length": str(fetch.MAX_PDF_BYTES + 1)}
    route({"https://publisher.example": big})
    r = fetch.fetch_paper({**PAPER, "pmcid": None, "doi": None}, store, tmp_path)
    assert r["status"] == "failed" and "too large" in r["attempts"][0]["result"]
