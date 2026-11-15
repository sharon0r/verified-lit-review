# -*- coding: utf-8 -*-
"""
Polite access to official scholarly APIs, with a SQLite cache.

Design rules (why this does not get blocked):
  * official REST APIs only - no scraping of search-engine HTML pages;
  * an honest User-Agent that names the tool (set CONTACT_EMAIL to join OpenAlex's polite pool);
  * at most one request per second per host, and exponential back-off on 429 / 5xx,
    honouring the server's Retry-After header;
  * every search is cached in SQLite, so repeating a question costs no network calls.

    from verified_lit_review.db import Store
    from verified_lit_review.sources import search_europepmc
    papers, status = search_europepmc("telephone follow-up after discharge readmission", Store(), n=10)
"""
import os
import re
import time
import urllib.parse

import requests

TOOL = "verified-lit-review/0.2 (+https://github.com/sharon0r/verified-lit-review)"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

STOP = {"the", "and", "of", "in", "on", "for", "to", "a", "an", "with", "versus", "vs", "is", "are",
        "does", "do", "can", "what", "which", "how", "effect", "effects", "impact", "role", "study",
        "after", "among", "between", "reduce", "reduces", "improve", "improves"}


class SourceError(Exception):
    """The source could not be reached or returned something unusable."""


class PoliteClient:
    """requests.Session with a per-host rate limit and retry/back-off."""

    def __init__(self, min_interval=1.0, retries=3, timeout=45, session=None):
        self.min_interval, self.retries, self.timeout = min_interval, retries, timeout
        self.s = session or requests.Session()
        contact = os.environ.get("CONTACT_EMAIL")
        self.s.headers["User-Agent"] = TOOL + (f" mailto:{contact}" if contact else "")
        self._last = {}

    def _wait_turn(self, host):
        gap = time.monotonic() - self._last.get(host, 0)
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last[host] = time.monotonic()

    def get(self, url, **kw):
        host = urllib.parse.urlparse(url).netloc
        delay, last_err = 2.0, None
        for attempt in range(self.retries + 1):
            self._wait_turn(host)
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
            except requests.RequestException as e:      # no internet, DNS failure, timeout ...
                last_err = f"{e.__class__.__name__}"
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, min(int(retry_after), 60))
                else:
                    return r
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        raise SourceError(f"{host}: {last_err} after {self.retries + 1} attempts")


_client = None


def client():
    global _client
    if _client is None:
        _client = PoliteClient()
    return _client


def keywords(question):
    """Content words of a research question, for a title/abstract query."""
    return [w for w in re.findall(r"[A-Za-z][\w-]+", question) if w.lower() not in STOP]


def europepmc_query(question):
    words = keywords(question)
    if not words:
        raise ValueError("the question has no searchable English keywords")
    return " AND ".join(f'TITLE_ABS:"{w}"' for w in words) + " AND OPEN_ACCESS:y"


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def parse_europepmc(data):
    out = []
    for res in data.get("resultList", {}).get("result", []):
        pdfs = [ft["url"] for ft in res.get("fullTextUrlList", {}).get("fullTextUrl", [])
                if ft.get("documentStyle") == "pdf" and ft.get("availabilityCode") in ("OA", "F")
                and "europepmc.org" not in ft["url"]]          # europepmc.org pages sit behind a bot check
        out.append({
            "source": "europepmc",
            "source_id": f"{res.get('source')}:{res.get('id')}",
            "title": _clean(res.get("title")).rstrip("."),
            "authors": res.get("authorString", ""),
            "journal": res.get("journalInfo", {}).get("journal", {}).get("title", ""),
            "year": res.get("pubYear", ""),
            "doi": res.get("doi"),
            "pmcid": res.get("pmcid"),
            "abstract": _clean(res.get("abstractText")) or None,
            "citations": res.get("citedByCount", 0),
            "pdf_urls": pdfs,
        })
    return out


def search_europepmc(question, store, n=10, max_age_days=30, raw_query=None):
    """Search Europe PMC (open-access papers only). Returns (papers, status).

    status is 'cache', 'network' or 'cache (stale, offline)'. Raises SourceError only when the
    network fails and there is no cached answer at all.
    """
    q = raw_query or europepmc_query(question)
    key = f"{q}|n={n}"
    cached = store.get_cached_search("europepmc", key, max_age_days)
    if cached is not None:
        return cached, "cache"
    # no "sort": the default is relevance ranking (an explicit sort=RELEVANCE is answered with HTTP 503)
    params = {"query": q, "format": "json", "resultType": "core", "pageSize": n}
    try:
        r = client().get(EPMC_SEARCH, params=params)
        papers = parse_europepmc(r.json())
    except (SourceError, ValueError) as e:
        stale = store.get_cached_search("europepmc", key, max_age_days=10**6)
        if stale is not None:
            return stale, "cache (stale, offline)"
        raise SourceError(f"Europe PMC unavailable and nothing cached: {e}") from e
    store.put_cached_search("europepmc", key, papers)
    for p in papers:
        store.upsert_paper(p)
    return papers, "network"
