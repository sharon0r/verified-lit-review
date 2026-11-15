# -*- coding: utf-8 -*-
"""
REST API, so other systems (e.g. a hospital portal) can request a verified review.

    API_TOKEN=... uvicorn verified_lit_review.api:app --port 8000   # docs at http://localhost:8000/docs
    Clients send the token in the X-API-Key header (an SSO / OAuth gateway would replace it in a hospital).

    POST /reviews      {"question": "...", "n_papers": 3, "language": "he"}  -> report + verification
    GET  /runs         recent runs
    GET  /runs/{id}    one run with its audit trail (every quote check)
    GET  /health
"""
import hmac
import os
import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import service

app = FastAPI(title="Verified Literature Review API",
              description="Agentic RAG over open-access papers; every quote is verified against its PDF page. "
                          "Every endpoint except /health needs the X-API-Key header.",
              version="0.2.1")


def require_api_key(x_api_key: str = Header(default="")):
    """Secure by default: without API_TOKEN configured on the server, the API refuses every call."""
    expected = os.environ.get("API_TOKEN", "")
    if not expected:
        raise HTTPException(503, "API disabled: set API_TOKEN on the server")
    if not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "missing or wrong X-API-Key")


class ReviewRequest(BaseModel):
    question: str = Field(min_length=5, max_length=500)
    n_papers: int = Field(3, ge=1, le=5)
    language: Literal["he", "en"] = "he"


class Rejected(BaseModel):
    claimed_page: int
    found_on_pages: list[int]
    text: str


class ReviewResponse(BaseModel):
    run_id: int
    papers: int
    verified_quotes: int
    rejected_quotes: list[Rejected]
    cost_usd: float
    seconds: float
    report_markdown: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reviews", response_model=ReviewResponse, dependencies=[Depends(require_api_key)])
def create_review(req: ReviewRequest):
    if service.runs_since(time.time() - 86400) >= int(os.environ.get("DAILY_RUN_LIMIT", "10")):
        raise HTTPException(429, "daily run limit reached")
    try:
        r = service.run_review(req.question, req.n_papers, req.language)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:                       # agent could not finish / model declined
        raise HTTPException(502, str(e))
    return ReviewResponse(run_id=r["run_id"], papers=r["papers"], verified_quotes=r["verified"],
                          rejected_quotes=[Rejected(claimed_page=q["claimed_page"], found_on_pages=q["found_on_pages"],
                                                    text=q["text"]) for q in r["rejected"]],
                          cost_usd=r["cost_usd"], seconds=r["seconds"], report_markdown=r["report_markdown"])


@app.get("/runs", dependencies=[Depends(require_api_key)])
def runs(limit: int = Query(20, ge=1, le=100)):
    return service.list_runs(limit)


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def run(run_id: int):
    r = service.get_run(run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r
