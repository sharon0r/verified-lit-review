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
import threading
import time
import urllib.parse

import requests

TOOL = "verified-lit-review/0.2 (+https://github.com/sharon0r/verified-lit-review)"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"      # biomedicine and life sciences
OPENALEX_SEARCH = "https://api.openalex.org/works"                           # every discipline
OPENALEX_FIELDS = ("id,doi,display_name,type,authorships,publication_year,primary_location,"
                   "abstract_inverted_index,cited_by_count,best_oa_location,locations,ids")

# Quality filters: peer-reviewed journal literature only.
#   OpenAlex: articles and reviews, in journals, not retracted, in CWTS "core" journals (international,
#             citation-based selection that leaves out marginal and predatory venues); open access.
#   Europe PMC: open access, no preprints (SRC:PPR), no retracted publications.
OPENALEX_FILTER = ("open_access.is_oa:true,type:article|review,primary_location.source.type:journal,"
                   "is_retracted:false,primary_location.source.is_core:true")
EPMC_FILTER = 'OPEN_ACCESS:y AND NOT SRC:PPR AND NOT PUB_TYPE:"retracted publication"'

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
        self._lock = threading.Lock()

    def _wait_turn(self, host):
        with self._lock:                         # one shared client serves every web-UI session (threads)
            gap = time.monotonic() - self._last.get(host, 0)
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last[host] = time.monotonic()

    def get(self, url, retries=None, timeout=None, **kw):
        """retries/timeout override the defaults for one call (PDF downloads use short ones)."""
        retries = self.retries if retries is None else retries
        host = urllib.parse.urlparse(url).netloc
        delay, last_err = 2.0, None
        for attempt in range(retries + 1):
            self._wait_turn(host)
            try:
                r = self.s.get(url, timeout=timeout or self.timeout, **kw)
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
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        raise SourceError(f"{host}: {last_err} after {retries + 1} attempts")


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
    return " AND ".join(f'TITLE_ABS:"{w}"' for w in words) + " AND " + EPMC_FILTER


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def author_list(names, limit=8):
    """The first `limit` authors, with "et al." so a shortened list is not taken as complete."""
    return ", ".join(names[:limit]) + (", et al." if len(names) > limit else "")


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
    if papers:                                   # an empty answer is not cached (may be a transient glitch)
        store.put_cached_search("europepmc", key, papers)
    for p in papers:
        store.upsert_paper(p)
    return papers, "network"


# ------------------------------------------------------------------ OpenAlex (all disciplines)
def _abstract(inverted):
    """OpenAlex ships abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return None
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def parse_openalex(data):
    out = []
    for w in data.get("results", []):
        pdfs = []
        for loc in [w.get("best_oa_location")] + (w.get("locations") or []):
            u = (loc or {}).get("pdf_url")
            if u and u not in pdfs:
                pdfs.append(u)
        pmcid = ((w.get("ids") or {}).get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1] or None
        source = ((w.get("primary_location") or {}).get("source") or {})
        out.append({
            "source": "openalex",
            "source_id": w["id"].rsplit("/", 1)[-1],
            "title": _clean(w.get("display_name")).rstrip("."),
            "authors": author_list([a["author"]["display_name"] for a in (w.get("authorships") or [])
                                    if a.get("author", {}).get("display_name")]),
            "journal": source.get("display_name") or "",
            "year": str(w.get("publication_year") or ""),
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "pmcid": pmcid if pmcid and pmcid.upper().startswith("PMC") else None,
            "abstract": _abstract(w.get("abstract_inverted_index")),
            "citations": w.get("cited_by_count", 0),
            "pdf_urls": pdfs,
        })
    return out


def search_openalex(question, store, n=10, max_age_days=30):
    """Search OpenAlex (all disciplines, open-access works only). Returns (papers, status)."""
    q = " ".join(keywords(question)) or question
    key = f"{q}|n={n}"
    cached = store.get_cached_search("openalex", key, max_age_days)
    if cached is not None:
        return cached, "cache"
    params = {"search": q, "filter": OPENALEX_FILTER, "per-page": n, "select": OPENALEX_FIELDS}
    if os.environ.get("CONTACT_EMAIL"):
        params["mailto"] = os.environ["CONTACT_EMAIL"]          # OpenAlex "polite pool"
    try:
        papers = parse_openalex(client().get(OPENALEX_SEARCH, params=params).json())
    except (SourceError, ValueError) as e:
        stale = store.get_cached_search("openalex", key, max_age_days=10**6)
        if stale is not None:
            return stale, "cache (stale, offline)"
        raise SourceError(f"OpenAlex unavailable and nothing cached: {e}") from e
    if papers:                                   # an empty answer is not cached (may be a transient glitch)
        store.put_cached_search("openalex", key, papers)
    for p in papers:
        store.upsert_paper(p)
    return papers, "network"


# ------------------------------------------------------------------ PubMed (systematic reviews only)
# Europe PMC already contains PubMed, so this source is not about coverage: PubMed's publication-type
# index finds the most recent systematic reviews and meta-analyses (incl. Cochrane) that a keyword
# search ranks too low - the gap that expert reviewers found in earlier reports.
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
REVIEWS_YEARS = 10


def pubmed_term(question):
    words = keywords(question)
    if not words:
        raise ValueError("the question has no searchable English keywords")
    since = time.gmtime().tm_year - REVIEWS_YEARS
    return (" ".join(words) + " AND (systematic review[pt] OR meta-analysis[pt]) AND free full text[sb]"
            f" AND {since}:3000[dp] NOT retracted publication[pt]")


def parse_pubmed_xml(xml_text):
    import xml.etree.ElementTree as ET
    out = []
    for art in ET.fromstring(xml_text).findall(".//PubmedArticle"):
        ids = {i.get("IdType"): (i.text or "").strip() for i in art.findall(".//PubmedData/ArticleIdList/ArticleId")}
        authors = [f"{a.findtext('LastName', '')} {a.findtext('Initials', '')}".strip()
                   for a in art.findall(".//AuthorList/Author") if a.findtext("LastName")]
        abstract = " ".join("".join(t.itertext()).strip() for t in art.findall(".//Abstract/AbstractText"))
        year = art.findtext(".//JournalIssue/PubDate/Year") or (art.findtext(".//JournalIssue/PubDate/MedlineDate") or "")[:4]
        out.append({
            "source": "pubmed",
            "source_id": f"PMID:{ids.get('pubmed', '')}",
            "title": _clean("".join(art.find(".//ArticleTitle").itertext())).rstrip("."),
            "authors": author_list(authors),
            "journal": art.findtext(".//Journal/Title") or "",
            "year": year,
            "doi": ids.get("doi") or None,
            "pmcid": ids.get("pmc") or None,
            "abstract": _clean(abstract) or None,
            "pub_types": [p.text for p in art.findall(".//PublicationType")],
            "pdf_urls": [],                      # downloaded via PMC on AWS (pmcid) or OpenAlex (doi)
        })
    return out


def search_pubmed_reviews(question, store, n=10, max_age_days=30):
    """Recent systematic reviews / meta-analyses with free full text, from PubMed (NCBI E-utilities)."""
    term = pubmed_term(question)
    key = f"{term}|n={n}"
    cached = store.get_cached_search("pubmed", key, max_age_days)
    if cached is not None:
        return cached, "cache"
    ident = {"tool": "verified-lit-review"}
    if os.environ.get("CONTACT_EMAIL"):
        ident["email"] = os.environ["CONTACT_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        ident["api_key"] = os.environ["NCBI_API_KEY"]
    try:
        ids = client().get(PUBMED_SEARCH, params={"db": "pubmed", "term": term, "retmax": n, "retmode": "json",
                                                  **ident}).json()["esearchresult"]["idlist"]
        papers = parse_pubmed_xml(client().get(PUBMED_FETCH, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "xml", **ident}).text) if ids else []
    except (SourceError, ValueError, KeyError) as e:
        stale = store.get_cached_search("pubmed", key, max_age_days=10**6)
        if stale is not None:
            return stale, "cache (stale, offline)"
        raise SourceError(f"PubMed unavailable and nothing cached: {e}") from e
    if papers:
        store.put_cached_search("pubmed", key, papers)
    for p in papers:
        store.upsert_paper(p)
    return papers, "network"


SEARCHERS = {"europepmc": search_europepmc, "openalex": search_openalex, "pubmed_reviews": search_pubmed_reviews}
