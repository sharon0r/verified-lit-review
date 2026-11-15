# -*- coding: utf-8 -*-
"""
Download open-access PDFs and index their pages in SQLite.

For each paper the open routes are tried in order, and a file is kept only if it really is a PDF:
  1. PMC Open Access dataset on AWS (by PMCID) - the most reliable route for biomedical papers
  2. links that came with the search result (Europe PMC full-text links)
  3. OpenAlex open-access locations (by DOI)
Publisher sites usually refuse scripted downloads even for open papers; such papers are skipped.

Every page's text is stored in the `pages` FTS5 table (db.py), so later steps retrieve passages
with SQL instead of re-reading PDFs. A paper already downloaded and indexed is not fetched again.

    from verified_lit_review.fetch import fetch_papers
    results = fetch_papers(papers, store, pdf_dir="pdfs", want=3)
"""
import hashlib
import ipaddress
import logging
import os
import re
import socket
import urllib.parse

import pymupdf

from .sources import SourceError, client

log = logging.getLogger(__name__)

PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com/"
OPENALEX = "https://api.openalex.org/works/doi:"
MAX_PDF_BYTES = 40 * 1024 * 1024


def safe_name(paper_id, max_len=50):
    """Readable, filesystem-safe and unique: '10.1000/a.b' and '10.1000/ab' must not collide."""
    slug = re.sub(r"\s+", "_", re.sub(r"[^\w\s-]", "", paper_id or "", flags=re.ASCII).strip())[:max_len]
    return f"{slug.rstrip('_') or 'paper'}_{hashlib.sha1((paper_id or '').encode()).hexdigest()[:8]}"


def is_public_url(url):
    """Refuse non-http(s) URLs and hosts that resolve to private, loopback or link-local addresses (SSRF)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except (socket.gaierror, UnicodeError):
        return True                      # unresolvable: the request itself will fail harmlessly
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _route(fn, arg, empty):
    """Run one route lookup; a network failure on one route must not stop the others."""
    try:
        return fn(arg)
    except SourceError as e:
        log.warning("route    %s(%s) failed: %s", fn.__name__, arg, e)
        return empty


# ------------------------------------------------------------------ routes
def pmc_s3_url(pmcid):
    """URL of the full-article PDF in the PMC Open Access dataset on AWS, or None."""
    if not pmcid:
        return None
    r = client().get(PMC_S3, params={"list-type": "2", "prefix": pmcid + "."})
    if r.status_code != 200:
        return None
    keys = [k for k in re.findall(r"<Key>(.*?)</Key>", r.text)
            if re.fullmatch(rf"{pmcid}\.\d+/{pmcid}\.\d+\.pdf", k)]     # article, not supplements
    return PMC_S3 + sorted(keys)[-1] if keys else None


def openalex_urls(doi):
    if not doi:
        return []
    r = client().get(OPENALEX + doi)
    if r.status_code != 200:
        return []
    work = r.json()
    urls = []
    for loc in [work.get("best_oa_location")] + (work.get("locations") or []):
        u = (loc or {}).get("pdf_url")
        if u and u not in urls:
            urls.append(u)
    return urls


def candidate_urls(paper):
    """Yield (route label, url) lazily, so later routes cost nothing when an earlier one works."""
    seen = set()

    def fresh(label, urls):
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                yield label, u

    yield from fresh("PMC open-data (AWS)", [_route(pmc_s3_url, paper.get("pmcid"), None)])
    yield from fresh("search result link", paper.get("pdf_urls") or [])
    yield from fresh("OpenAlex", _route(openalex_urls, paper.get("doi"), []))


# ------------------------------------------------------------------ download + index
def download_pdf(url, dest):
    """Save url to dest only if the body is a real PDF under MAX_PDF_BYTES. Returns (ok, message).

    The body is streamed and abandoned as soon as it exceeds the limit, and written to a temporary
    file that replaces dest atomically, so a half-downloaded file never looks like a paper.
    """
    if not is_public_url(url):
        return False, "refused: not a public http(s) address"
    try:
        r = client().get(url, allow_redirects=True, stream=True)
    except SourceError as e:
        return False, str(e)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    size = (r.headers or {}).get("Content-Length", "")
    if size.isdigit() and int(size) > MAX_PDF_BYTES:
        return False, f"too large ({int(size) // 1_000_000} MB)"
    body = bytearray()
    for chunk in (r.iter_content(1 << 16) if hasattr(r, "iter_content") else [r.content]):
        body += chunk
        if len(body) > MAX_PDF_BYTES:
            return False, f"too large (> {MAX_PDF_BYTES // 1_000_000} MB)"
    if not body.startswith(b"%PDF-"):
        return False, "not a PDF (probably an HTML landing page or a bot check)"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, dest)
    return True, f"{len(body) // 1000} KB"


def extract_pages(pdf_path):
    """Text of every page, index 0 = page 1. Raises ValueError for a PDF without a text layer."""
    with pymupdf.open(pdf_path) as doc:
        pages = [p.get_text() for p in doc]
    if sum(len(t.strip()) for t in pages) < 200:
        raise ValueError("no text layer (scanned PDF? would need OCR)")
    return pages


def fetch_paper(paper, store, pdf_dir):
    """Download + index one paper. Returns a result dict with status 'cached', 'ok' or 'failed'."""
    paper_id = store.upsert_paper(paper)
    result = {"paper_id": paper_id, "title": paper["title"], "attempts": []}

    known = store.get_paper(paper_id)
    if known and known["pdf_path"] and os.path.isfile(known["pdf_path"]) and store.page_count(paper_id):
        log.info("cached   %s (%d pages)", paper_id, store.page_count(paper_id))
        return {**result, "status": "cached", "pdf_path": known["pdf_path"], "pages": store.page_count(paper_id)}

    dest = os.path.join(pdf_dir, safe_name(paper_id) + ".pdf")
    try:
        for label, url in candidate_urls(paper):
            ok, msg = download_pdf(url, dest)
            result["attempts"].append({"route": label, "url": url, "result": msg})
            log.info("%-8s %s via %s: %s", "ok" if ok else "skip", paper_id, label, msg)
            if not ok:
                continue
            try:
                pages = extract_pages(dest)
            except (ValueError, RuntimeError) as e:     # RuntimeError: PyMuPDF cannot open the file
                result["attempts"][-1]["result"] = f"unusable PDF: {e}"
                log.warning("skip     %s: unusable PDF: %s", paper_id, e)
                os.remove(dest)
                continue
            store.add_pages(paper_id, pages)
            store.set_pdf(paper_id, dest, route=label)
            return {**result, "status": "ok", "pdf_path": dest, "pages": len(pages), "route": label}
    except SourceError as e:                            # a route lookup itself failed (network)
        result["attempts"].append({"route": "lookup", "url": "", "result": str(e)})
        log.warning("network  %s: %s", paper_id, e)
    log.warning("failed   %s: no open PDF route worked", paper_id)
    return {**result, "status": "failed"}


def fetch_papers(papers, store, pdf_dir="pdfs", want=3):
    """Go down the ranked list until `want` papers are downloaded and indexed.

    Returns the list of per-paper results (successes and failures), for logging and the UI.
    """
    results, got = [], 0
    for paper in papers:
        if got >= want:
            break
        r = fetch_paper(paper, store, pdf_dir)
        results.append(r)
        got += r["status"] in ("ok", "cached")
    log.info("indexed %d/%d papers (%d tried)", got, want, len(results))
    return results
