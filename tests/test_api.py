"""REST API tests: the real service and database, a scripted fake model (no key, no network)."""
from types import SimpleNamespace as NS

import pymupdf
import pytest
from fastapi.testclient import TestClient

from verified_lit_review import api, service
from verified_lit_review.db import Store

PAGE = "Results. Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 12.5% (p<0.01)."
GOOD = "Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 12.5% (p<0.01)."
FAKE = "Telephone follow-up within 72 hours reduced 30-day readmission from 18.2% to 10.5% (p<0.01)."


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.beta = NS(messages=NS(create=lambda **kw: self.script.pop(0)))


def call(name, **args):
    return NS(content=[NS(type="tool_use", id=f"c{name}", name=name, input=args)], stop_reason="tool_use",
              usage=NS(input_tokens=500, output_tokens=100))


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 540, 760), PAGE, fontsize=10)
    pdf = tmp_path / "p.pdf"
    doc.save(pdf)
    s = Store(db)
    pid = s.upsert_paper({"doi": "10.1/api", "title": "Calls", "source": "test"})
    s.add_pages(pid, [PAGE])
    s.set_pdf(pid, str(pdf))
    s.close()

    art = {"paper_id": pid, "topic": "t", "study_type": "RCT", "bullets": ["b"], "relevance": "r",
           "quotes": [{"page": 1, "text": GOOD}, {"page": 1, "text": FAKE}]}
    script = [call("fetch_paper", paper_id=pid), call("submit_report", articles=[art]),
              call("submit_report", articles=[art])]
    real = service.run_review
    monkeypatch.setattr(service, "DB_PATH", db)
    monkeypatch.setattr(service, "run_review", lambda q, n, lang: real(
        q, n, lang, client=FakeClient(script), data_dir=str(tmp_path), db_path=db))
    return TestClient(api.app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_review_returns_verified_report_and_audit_trail(client):
    r = client.post("/reviews", json={"question": "Does telephone follow-up reduce readmission?", "n_papers": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["verified_quotes"] == 1 and len(body["rejected_quotes"]) == 1
    assert GOOD in body["report_markdown"] and FAKE not in body["report_markdown"]

    run = client.get(f"/runs/{body['run_id']}").json()
    assert run["summary"] == {"total": 2, "verified": 1, "rejected": 1}
    assert [q["verified"] for q in run["quote_checks"]] == [True, False]
    assert client.get("/runs").json()[0]["run_id"] == body["run_id"]


def test_invalid_requests_are_rejected(client):
    assert client.post("/reviews", json={"question": "hi"}).status_code == 422
    assert client.post("/reviews", json={"question": "valid question here", "n_papers": 50}).status_code == 422
    assert client.get("/runs/999").status_code == 404
