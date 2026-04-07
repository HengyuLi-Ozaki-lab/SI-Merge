#!/usr/bin/env python3
"""
SI Merge — Automatically find, download, and merge Supplementary Information
into journal article PDFs with cross-reference links.

Usage (CLI):
    python si_merge.py <article.pdf> [-o output.pdf] [--doi DOI]

Usage (library):
    from si_merge import run_merge
    result = run_merge(pdf_bytes, on_progress=my_callback)
"""

import argparse
import io
import os
import re
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)
import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CROSSREF_UA = "SI-Merger/1.0 (mailto:si-merger@example.com)"

ProgressCallback = Callable[[int, str, str], None]  # (step, status, detail)


def _noop_progress(step: int, status: str, detail: str = "") -> None:
    pass


# ---------------------------------------------------------------------------
# HTTP client — curl_cffi (bypasses Cloudflare) with requests fallback
# ---------------------------------------------------------------------------

_session: dict[str, object] = {}

_IMPERSONATE_PROFILES = ("chrome", "safari15_5")


def _get_session(profile: str = "chrome"):
    """Lazily create a persistent session for cookie/referer handling."""
    if profile in _session:
        return _session[profile]
    try:
        from curl_cffi import requests as cffi_requests
        sess = cffi_requests.Session()
        sess._impersonate = profile
        _session[profile] = sess
        return sess
    except ImportError:
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT})
        _session[profile] = sess
        return sess


def _http_get(url: str, *, timeout: int = 30, allow_redirects: bool = True,
              headers: dict | None = None, referer: str | None = None) -> requests.Response:
    """
    GET with browser-like TLS fingerprint via curl_cffi.
    Uses a persistent session to carry cookies across requests (needed for Wiley etc.).
    When the primary profile (Chrome) is blocked (403), automatically retries with
    alternative profiles (Safari) before giving up.
    Falls back to plain requests if curl_cffi is unavailable.
    """
    hdrs = dict(headers or {})
    if referer:
        hdrs["Referer"] = referer

    try:
        from curl_cffi import requests as cffi_requests

        last_exc = None
        for profile in _IMPERSONATE_PROFILES:
            session = _get_session(profile)
            if not isinstance(session, cffi_requests.Session):
                break
            try:
                resp = session.get(
                    url, impersonate=profile, timeout=timeout,
                    allow_redirects=allow_redirects, headers=hdrs or None,
                )
                if resp.status_code != 403 or profile == _IMPERSONATE_PROFILES[-1]:
                    return resp
            except Exception as e:
                last_exc = e
                if profile == _IMPERSONATE_PROFILES[-1]:
                    raise
        else:
            if last_exc:
                raise last_exc
            return resp  # type: ignore[possibly-undefined]
    except ImportError:
        pass

    session = _get_session("chrome")
    if hdrs:
        session.headers.update(hdrs)
    return session.get(url, timeout=timeout, allow_redirects=allow_redirects)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SIFile:
    url: str
    label: str  # e.g. "Supplementary Information", "Peer Review File"
    local_path: str = ""


@dataclass
class SIReference:
    """A reference to SI content found in the main article text."""
    text: str          # matched text, e.g. "Supplementary Fig. S1"
    page_idx: int      # 0-based page in original article
    rect: fitz.Rect | None = None
    target_key: str = ""  # normalized key, e.g. "figure_1"


@dataclass
class SIAnchor:
    """A heading/caption in the SI document that can be linked to."""
    text: str
    page_idx: int      # 0-based page in SI document
    rect: fitz.Rect | None = None
    key: str = ""       # normalized key, e.g. "figure_1"


# ---------------------------------------------------------------------------
# DOI Extraction
# ---------------------------------------------------------------------------

DOI_RE = re.compile(r'10\.\d{4,9}/[^\s,;\"\'>\]}{)]+', re.IGNORECASE)


def extract_doi_from_metadata(doc: fitz.Document) -> str | None:
    """Try to extract DOI from PDF metadata fields."""
    meta = doc.metadata or {}
    for key in ("subject", "title", "keywords", "creator", "producer"):
        val = meta.get(key, "") or ""
        m = DOI_RE.search(val)
        if m:
            return m.group(0).rstrip(".")
    return None


def extract_doi_from_text(doc: fitz.Document, max_pages: int = 3) -> str | None:
    """Try to extract DOI from PDF text content."""
    for i in range(min(max_pages, len(doc))):
        text = doc[i].get_text()
        if text.strip():
            m = DOI_RE.search(text)
            if m:
                return m.group(0).rstrip(".")
    return None


def extract_doi(doc: fitz.Document) -> str | None:
    return extract_doi_from_metadata(doc) or extract_doi_from_text(doc)


# ---------------------------------------------------------------------------
# Article URL Resolution
# ---------------------------------------------------------------------------

def resolve_article_url(doi: str) -> str | None:
    """Resolve a DOI to the publisher landing page URL."""
    try:
        resp = _http_get(f"https://doi.org/{doi}", timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            return resp.url
        # Some publishers block bots but the redirect still provides the URL
        if resp.url and resp.url != f"https://doi.org/{doi}":
            return resp.url
    except Exception:
        pass
    # Fallback: construct URL from doi.org redirect without following to final destination
    try:
        resp = requests.get(
            f"https://doi.org/{doi}",
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
            timeout=10,
        )
        location = resp.headers.get("Location")
        if location:
            return location
    except Exception:
        pass
    return None


def get_article_pdf_url(doi: str) -> str | None:
    """Get the direct PDF URL from CrossRef metadata."""
    try:
        resp = requests.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": CROSSREF_UA},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json().get("message", {})
            for link in data.get("link", []):
                if link.get("content-type") == "application/pdf":
                    return link["URL"]
    except requests.RequestException:
        pass
    return None


# ---------------------------------------------------------------------------
# Article PDF Download (for browser extension / DOI-based merge)
# ---------------------------------------------------------------------------

_PDF_URL_PATTERNS: dict[str, str] = {
    "nature.com":       "{article_url}.pdf",
    "springer.com":     "{article_url}.pdf",
    "pubs.acs.org":     "https://pubs.acs.org/doi/pdf/{doi}",
    "onlinelibrary.wiley.com": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
    "science.org":      "https://www.science.org/doi/pdf/{doi}",
    "pnas.org":         "https://www.pnas.org/doi/pdf/{doi}",
}


def download_article_pdf(
    doi: str,
    work_dir: str,
    on_progress: ProgressCallback = _noop_progress,
) -> tuple[str, str]:
    """
    Download the main article PDF given a DOI.

    Returns (pdf_path, article_url).
    Raises RuntimeError if the PDF cannot be obtained.
    """
    on_progress(1, "started", f"Resolving DOI: {doi}")
    article_url = resolve_article_url(doi)
    if not article_url:
        raise RuntimeError(f"Could not resolve DOI {doi} to an article URL.")
    on_progress(1, "searching", f"Article: {article_url}")

    pdf_url = None

    # Strategy 1: citation_pdf_url meta tag
    try:
        resp = _http_get(article_url, timeout=20)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
            if meta and meta.get("content"):
                pdf_url = meta["content"]
    except Exception:
        pass

    # Strategy 2: publisher-specific URL patterns
    if not pdf_url:
        domain = urllib.parse.urlparse(article_url).netloc
        for pub_domain, pattern in _PDF_URL_PATTERNS.items():
            if pub_domain in domain:
                pdf_url = pattern.format(article_url=article_url.rstrip("/"), doi=doi)
                break

    # Strategy 3: CrossRef link
    if not pdf_url:
        pdf_url = get_article_pdf_url(doi)

    if not pdf_url:
        raise RuntimeError(
            f"Could not find a PDF download link for DOI {doi}. "
            "The article may require institutional access."
        )

    on_progress(1, "downloading", f"Downloading article PDF...")
    try:
        resp = _http_get(pdf_url, timeout=60, allow_redirects=True)
    except Exception as e:
        raise RuntimeError(f"Failed to download article PDF: {e}")

    if resp.status_code == 403:
        raise RuntimeError(
            "Publisher blocked the PDF download (HTTP 403). "
            "The article likely requires institutional access. "
            "Please download the PDF manually and use the web app."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"PDF download failed with status {resp.status_code}.")

    content = resp.content
    if not content[:5].startswith(b"%PDF"):
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype.lower():
            raise RuntimeError(
                "Received an HTML page instead of a PDF. "
                "The article likely requires institutional access."
            )

    pdf_path = os.path.join(work_dir, "article.pdf")
    with open(pdf_path, "wb") as f:
        f.write(content)
    on_progress(1, "done", f"Article PDF downloaded ({len(content) // 1024} KB)")
    return pdf_path, article_url


# ---------------------------------------------------------------------------
# SI Discovery — publisher-specific scrapers
# ---------------------------------------------------------------------------

def _scrape_springer_nature(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Scrape Springer Nature / Nature Communications SI links."""
    results = []
    si_section = soup.find("section", {"data-title": "Supplementary information"})
    if not si_section:
        for s in soup.find_all("section"):
            heading = s.find(["h2", "h3"])
            if heading and "supplementary" in heading.get_text().lower():
                si_section = s
                break
    if si_section:
        for link in si_section.find_all("a", href=True):
            href = link["href"]
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            label = link.get_text(strip=True)
            if any(ext in href.lower() for ext in [".pdf", ".doc", ".xlsx", ".zip"]):
                results.append(SIFile(url=href, label=label))
    return results


def _scrape_acs(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Scrape ACS Publications SI links."""
    results = []
    seen_urls = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if "suppl_file" in href.lower() and href.lower().endswith(".pdf"):
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            if href not in seen_urls:
                seen_urls.add(href)
                label = text if text and text.lower() != "pdf" else "Supporting Information"
                results.append(SIFile(url=href, label=label))
    return results


def _scrape_elsevier(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Scrape Elsevier / ScienceDirect SI links."""
    results = []
    seen_urls = set()

    # Method 1: find mmc links in page HTML
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if "mmc" in href.lower() and any(href.lower().endswith(e) for e in (".pdf", ".docx", ".doc", ".xlsx")):
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            if href not in seen_urls:
                seen_urls.add(href)
                results.append(SIFile(url=href, label=text or "Supplementary Material"))

    # Method 2: ScienceDirect may hide SI behind JS.  Probe CDN URL pattern.
    if not results:
        pii_match = re.search(r'/pii/([A-Z0-9]+)', base_url, re.IGNORECASE)
        if pii_match:
            pii = pii_match.group(1)
            for n in range(1, 6):
                url = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}-mmc{n}.pdf"
                try:
                    resp = _http_get(url, timeout=10)
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        ctype = resp.headers.get("content-type", "").lower()
                        if "pdf" in ctype or "octet" in ctype:
                            results.append(SIFile(url=url, label=f"Supplementary Material {n}"))
                        else:
                            break
                    else:
                        break
                except Exception:
                    break

    return results


def _scrape_pnas_science(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Scrape PNAS / Science (AAAS) SI links. Both use similar Atypon-based platforms."""
    results = []
    seen_urls = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if "/doi/suppl/" in href and "suppl_file" in href:
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            if href not in seen_urls:
                seen_urls.add(href)
                label = text if text and text.lower() not in ("download", "pdf") else "Supplementary Material"
                results.append(SIFile(url=href, label=label))

    # Also check the supplementary materials section
    supp_section = soup.find("section", class_=re.compile(r"core-supplementary-materials?"))
    if supp_section:
        for link in supp_section.find_all("a", href=True):
            href = link["href"]
            if href.startswith("/") or href.startswith("http"):
                if not href.startswith("http"):
                    href = urllib.parse.urljoin(base_url, href)
                ext = _get_file_ext(href) or Path(urllib.parse.urlparse(href).path).suffix.lower()
                if ext in SUPPORTED_SI_EXTENSIONS or "suppl_file" in href:
                    if href not in seen_urls:
                        seen_urls.add(href)
                        results.append(SIFile(url=href, label=link.get_text(strip=True) or "SI Appendix"))

    return results


def _scrape_wiley(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Scrape Wiley Online Library SI links."""
    results = []
    seen_urls = set()
    supported = {".pdf", ".doc", ".docx", ".xlsx", ".xls", ".zip"}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if "downloadSupplement" in href or "suppl" in href.lower():
            ext = Path(urllib.parse.urlparse(href).path).suffix.lower()
            if ext in supported or "downloadSupplement" in href:
                if not href.startswith("http"):
                    href = urllib.parse.urljoin(base_url, href)
                if href not in seen_urls:
                    seen_urls.add(href)
                    results.append(SIFile(url=href, label=text or "Supporting Information"))
    return results


def _scrape_generic(soup: BeautifulSoup, base_url: str) -> list[SIFile]:
    """Fallback: look for any links with supplementary/SI keywords."""
    results = []
    seen_urls = set()
    keywords = ["supplement", "supporting", "esm", "moesm", "si_file", "appendix",
                "suppdata", "suppl_file", "electronic supplementary"]
    file_exts = [".pdf", ".doc", ".docx", ".xlsx", ".xls", ".zip", ".csv"]
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        combined = (href + " " + text).lower()
        if any(kw in combined for kw in keywords):
            if any(ext in href.lower() for ext in file_exts):
                if not href.startswith("http"):
                    href = urllib.parse.urljoin(base_url, href)
                if href not in seen_urls:
                    seen_urls.add(href)
                    results.append(SIFile(url=href, label=text or "Supplementary File"))
    return results


PUBLISHER_SCRAPERS = {
    "nature.com": _scrape_springer_nature,
    "springer.com": _scrape_springer_nature,
    "link.springer.com": _scrape_springer_nature,
    "pubs.acs.org": _scrape_acs,
    "sciencedirect.com": _scrape_elsevier,
    "onlinelibrary.wiley.com": _scrape_wiley,
    "pnas.org": _scrape_pnas_science,
    "science.org": _scrape_pnas_science,
    "rsc.org": _scrape_generic,
}


def _resolve_elsevier_redirect(url: str, soup: BeautifulSoup) -> str | None:
    """Follow Elsevier linkinghub meta-refresh redirect to ScienceDirect."""
    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if meta:
        content = meta.get("content", "")
        m = re.search(r"Redirect=(https?[^&'\"]+)", content)
        if m:
            return urllib.parse.unquote(m.group(1))
    return None


def find_si_links(article_url: str, on_progress: ProgressCallback = _noop_progress) -> list[SIFile]:
    """Scrape the article landing page for SI download links."""
    on_progress(2, "searching", f"Scraping article page: {article_url}")
    resp = _http_get(article_url, timeout=30)

    if resp.status_code == 403:
        on_progress(2, "warning", f"Publisher blocked access (HTTP 403). Trying alternative methods...")
        return _fallback_si_discovery(article_url, on_progress)

    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Handle Elsevier linkinghub redirect pages
    if "linkinghub.elsevier.com" in article_url:
        redirect_url = _resolve_elsevier_redirect(article_url, soup)
        if redirect_url:
            on_progress(2, "searching", f"Following redirect to: {redirect_url}")
            article_url = redirect_url
            resp = _http_get(article_url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

    domain = urllib.parse.urlparse(article_url).netloc
    for pub_domain, scraper in PUBLISHER_SCRAPERS.items():
        if pub_domain in domain:
            results = scraper(soup, article_url)
            if results:
                return results

    return _scrape_generic(soup, article_url)


def _fallback_si_discovery(article_url: str, on_progress: ProgressCallback) -> list[SIFile]:
    """Try publisher-specific URL patterns when page scraping is blocked."""
    domain = urllib.parse.urlparse(article_url).netloc

    # Science / Science Advances / PNAS (Atypon platform)
    if "science.org" in domain or "pnas.org" in domain:
        doi_match = re.search(r'(10\.\d{4,}/\S+?)(?:[#?]|$)', article_url)
        if doi_match:
            doi = doi_match.group(1).rstrip("/")
            on_progress(2, "fallback", "Constructing Science/PNAS SI URL from DOI pattern")
            article_id = doi.split("/")[-1]
            journal_prefix = ""
            if "sciadv." in article_id or "science." in article_id:
                journal_prefix = article_id.split(".")[0] + "."
            slug = article_id if journal_prefix else article_id

            candidates = []
            for pattern in [
                f"{slug}_sm.pdf", f"{slug}_SM.pdf",
                f"{slug}-sm.pdf", f"{slug}-SM.pdf",
            ]:
                candidates.append(
                    f"https://www.{'science.org' if 'science.org' in domain else 'pnas.org'}"
                    f"/doi/suppl/{doi}/suppl_file/{pattern}"
                )

            for url in candidates:
                try:
                    resp = _http_get(url, timeout=15)
                    if _is_valid_file_response(resp, ".pdf"):
                        return [SIFile(url=url, label="Supplementary Materials")]
                except Exception:
                    continue

            base_url = f"https://www.{'science.org' if 'science.org' in domain else 'pnas.org'}/doi/suppl/{doi}/suppl_file/"
            on_progress(2, "info",
                        f"SI likely exists but is protected by Cloudflare. "
                        f"Try the Chrome extension or manually download from the article page.")

    # APS: supplemental material at /journal/supplemental/DOI
    if "aps.org" in domain:
        on_progress(2, "fallback", "Trying APS supplemental URL pattern")
        path = urllib.parse.urlparse(article_url).path
        doi_match = re.search(r'(10\.\d{4,}/\S+)', article_url)
        if doi_match:
            doi = doi_match.group(1)
            journal_codes = {"PhysRevLett": "prl", "PhysRevB": "prb", "PhysRevX": "prx",
                             "PhysRevMaterials": "prmaterials", "PhysRevE": "pre",
                             "PhysRevA": "pra", "RevModPhys": "rmp"}
            journal = doi.split("/")[1].split(".")[0]
            code = journal_codes.get(journal, journal.lower())
            suppl_url = f"https://journals.aps.org/{code}/supplemental/{doi}"
            resp = _http_get(suppl_url, timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                results = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if any(ext in href.lower() for ext in [".pdf", ".doc", ".zip", ".tar"]):
                        if not href.startswith("http"):
                            href = urllib.parse.urljoin(suppl_url, href)
                        results.append(SIFile(url=href, label=a.get_text(strip=True) or "Supplemental Material"))
                if results:
                    return results
            on_progress(2, "info", "No supplemental material found for this APS article")

    return []


# ---------------------------------------------------------------------------
# SI Download
# ---------------------------------------------------------------------------

SUPPORTED_SI_EXTENSIONS = {".pdf", ".doc", ".docx", ".xlsx", ".xls"}


def _get_file_ext(url: str) -> str:
    """Extract file extension from URL, checking both path and query parameters."""
    parsed = urllib.parse.urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in SUPPORTED_SI_EXTENSIONS:
        return ext
    # Check query parameters (e.g. Wiley: ?file=name.docx)
    params = urllib.parse.parse_qs(parsed.query)
    for key in ("file", "filename"):
        for val in params.get(key, []):
            ext = Path(val).suffix.lower()
            if ext in SUPPORTED_SI_EXTENSIONS:
                return ext
    # Check the full URL string as fallback
    for ext in SUPPORTED_SI_EXTENSIONS:
        if ext in url.lower():
            return ext
    return ""


_DOCX_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: Arial, Helvetica, sans-serif; font-size: 11pt;
       margin: 2cm; line-height: 1.5; }}
img  {{ max-width: 100%; height: auto; page-break-inside: avoid; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.8em 0;
         page-break-inside: auto; }}
td, th {{ border: 1px solid #999; padding: 4px 8px; font-size: 10pt; }}
h1 {{ font-size: 16pt; }} h2 {{ font-size: 14pt; }} h3 {{ font-size: 12pt; }}
p {{ margin: 0.4em 0; }}
</style></head><body>{body}</body></html>"""


def _convert_to_pdf(input_path: str, output_path: str) -> bool:
    """Convert a non-PDF file (docx, doc, xlsx) to PDF.
    Uses pure-Python mammoth + WeasyPrint (no local Office software needed).
    Falls back to LibreOffice headless for formats mammoth cannot handle.
    """
    import subprocess
    ext = Path(input_path).suffix.lower()
    if ext not in (".doc", ".docx", ".xlsx", ".xls"):
        return False

    # Strategy 1: mammoth + WeasyPrint (pure Python, no external software)
    if ext == ".docx":
        try:
            import mammoth
            from weasyprint import HTML
            with open(input_path, "rb") as f:
                result = mammoth.convert_to_html(f)
            full_html = _DOCX_HTML_TEMPLATE.format(body=result.value)
            HTML(string=full_html).write_pdf(output_path)
            if os.path.isfile(output_path) and os.path.getsize(output_path) > 100:
                return True
        except ImportError:
            pass
        except Exception:
            pass

    # Strategy 2: LibreOffice headless (handles .doc, .xlsx, and .docx fallback)
    for cmd in ["libreoffice", "soffice",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"]:
        try:
            subprocess.run(
                [cmd, "--headless", "--convert-to", "pdf", "--outdir",
                 str(Path(output_path).parent), input_path],
                capture_output=True, timeout=180,
            )
            expected = Path(output_path).parent / (Path(input_path).stem + ".pdf")
            if expected.is_file():
                if str(expected) != output_path:
                    expected.rename(output_path)
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return False


def _is_valid_file_response(resp, expected_ext: str) -> bool:
    """Check if an HTTP response actually contains the expected file, not HTML."""
    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        return False
    if expected_ext == ".pdf" and not resp.content[:5].startswith(b"%PDF-"):
        return False
    return True


def download_si_files(
    si_files: list[SIFile], output_dir: str,
    article_url: str = "",
    on_progress: ProgressCallback = _noop_progress,
) -> list[SIFile]:
    """Download SI files, convert non-PDF formats, and update their local_path."""
    os.makedirs(output_dir, exist_ok=True)
    downloaded = []

    for i, si in enumerate(si_files):
        ext = _get_file_ext(si.url)
        if ext not in SUPPORTED_SI_EXTENSIONS:
            on_progress(3, "skipping", f"Unsupported format ({ext}): {si.label}")
            continue

        on_progress(3, "downloading", f"Downloading: {si.label}")
        try:
            resp = _http_get(si.url, timeout=120, referer=article_url or None)
            resp.raise_for_status()
        except Exception as e:
            on_progress(3, "warning", f"Download failed for {si.label}: {e}")
            on_progress(3, "info", f"SI URL: {si.url}")
            continue

        if not ext:
            ext = ".pdf"

        if not _is_valid_file_response(resp, ext):
            on_progress(3, "warning",
                        f"Download blocked for {si.label} (publisher returned HTML instead of file). "
                        f"You can manually download the SI from: {si.url}")
            continue

        raw_path = os.path.join(output_dir, f"si_{i+1}_raw{ext}")
        with open(raw_path, "wb") as f:
            f.write(resp.content)

        if ext == ".pdf":
            final_path = raw_path
        else:
            on_progress(3, "converting", f"Converting {ext} to PDF...")
            final_path = os.path.join(output_dir, f"si_{i+1}.pdf")
            if not _convert_to_pdf(raw_path, final_path):
                on_progress(3, "warning", f"Could not convert {si.label} to PDF (install LibreOffice or MS Word)")
                continue

        si.local_path = final_path
        on_progress(3, "downloaded", f"Saved {si.label} ({len(resp.content) / 1024:.0f} KB)")
        downloaded.append(si)

    return downloaded


# ---------------------------------------------------------------------------
# Text Extraction (with fallback strategies)
# ---------------------------------------------------------------------------

def extract_text_direct(doc: fitz.Document) -> dict[int, str]:
    """Try direct text extraction from all pages. Returns {page_idx: text}."""
    result = {}
    for i in range(len(doc)):
        text = doc[i].get_text()
        if text.strip():
            result[i] = text
    return result


def extract_text_with_ocr(doc: fitz.Document) -> dict[int, str]:
    """Render pages to images and OCR. Returns {page_idx: text}."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        print("  Warning: pytesseract/Pillow not installed, OCR unavailable")
        return {}

    result = {}
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(img)
        if text.strip():
            result[i] = text
    return result


def redownload_article_pdf(
    doi: str, output_path: str, on_progress: ProgressCallback = _noop_progress,
) -> str | None:
    """Re-download the article PDF from the publisher for better text extraction."""
    pdf_url = get_article_pdf_url(doi)
    if not pdf_url:
        return None
    on_progress(4, "redownloading", f"Re-downloading article PDF from publisher")
    resp = _http_get(pdf_url, timeout=60)
    if resp.status_code == 200 and len(resp.content) > 1000:
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return output_path
    return None


def get_article_text(
    original_doc: fitz.Document, doi: str, work_dir: str,
    on_progress: ProgressCallback = _noop_progress,
) -> tuple[fitz.Document, dict[int, str]]:
    """
    Get text from the article, trying multiple strategies.
    Returns (doc_to_use_for_merging, page_texts).
    The returned doc may differ from original_doc if we re-downloaded.
    """
    on_progress(4, "extracting", "Extracting text from article PDF")
    texts = extract_text_direct(original_doc)
    total_chars = sum(len(t) for t in texts.values())

    if total_chars > 500:
        on_progress(4, "extracted", f"Extracted {total_chars} chars from {len(texts)} pages")
        return original_doc, texts

    on_progress(4, "fallback", "Direct extraction insufficient, trying re-download")
    redownloaded = redownload_article_pdf(doi, os.path.join(work_dir, "article_redownloaded.pdf"), on_progress)
    if redownloaded:
        new_doc = fitz.open(redownloaded)
        texts = extract_text_direct(new_doc)
        total_chars = sum(len(t) for t in texts.values())
        if total_chars > 500:
            on_progress(4, "extracted", f"Re-downloaded PDF: {total_chars} chars from {len(texts)} pages")
            return new_doc, texts

    on_progress(4, "ocr", "Text extraction failed, trying OCR")
    texts = extract_text_with_ocr(original_doc)
    total_chars = sum(len(t) for t in texts.values())
    if total_chars > 100:
        on_progress(4, "extracted", f"OCR extraction: {total_chars} chars from {len(texts)} pages")
    else:
        on_progress(4, "warning", "Could not extract text — cross-referencing will be limited")
    return original_doc, texts


# ---------------------------------------------------------------------------
# SI Reference Detection & Mapping
# ---------------------------------------------------------------------------

# Patterns that appear in the main article text (ordered by specificity)
MAIN_TEXT_SI_PATTERNS = [
    # Nature-style: "Supplementary Fig. S1", "Supplementary Figure S2a"
    (r'[Ss]upplementary\s+Fig(?:ure|\.)\s*S?(\d+)([a-z]?)', "figure"),
    (r'[Ss]upplementary\s+Figs?\.\s*S?(\d+)', "figure"),
    (r'[Ss]upplementary\s+Tables?\s*S?(\d+)', "table"),
    (r'[Ss]upplementary\s+Notes?\s*S?(\d+)', "note"),
    (r'[Ss]upplementary\s+(Method|Discussion|Data)\w*', "section"),
    # Standalone with S prefix (common across publishers)
    (r'Figure\s+S(\d+)([a-z]?)', "figure"),
    (r'Figures?\s+S(\d+)', "figure"),
    (r'Fig\.\s*S(\d+)([a-z]?)', "figure"),
    (r'Figs?\.\s*S(\d+)', "figure"),
    (r'Table\s+S(\d+)', "table"),
    (r'Tables\s+S(\d+)', "table"),
    # ACS-style: "Section S1"
    (r'Section\s+S(\d+)', "section"),
    # "Scheme S1"
    (r'Scheme\s+S(\d+)', "scheme"),
    # "Equation S1"
    (r'Equation\s+S(\d+)', "equation"),
]

# Patterns that appear in the SI document as headings/captions
SI_HEADING_PATTERNS = [
    # Nature-style: "Supplementary Figure 1"
    (r'Supplementary\s+Figure\s+(\d+)', "figure"),
    (r'Supplementary\s+Table\s+(\d+)', "table"),
    (r'Supplementary\s+Note\s+(\d+)', "note"),
    (r'Supplementary\s+Method', "section_method"),
    (r'Supplementary\s+Discussion', "section_discussion"),
    (r'Supplementary\s+Data\s*(\d*)', "data"),
    # ACS / general style: "Figure S1:", "Table S1:"
    (r'Figure\s+S(\d+)', "figure"),
    (r'Table\s+S(\d+)', "table"),
    (r'Scheme\s+S(\d+)', "scheme"),
    (r'Equation\s+S(\d+)', "equation"),
    # ACS section headings: "S1 DFT dataset" (standalone S-number at line start)
    (r'(?:^|\n)\s*S(\d+)\s+[A-Z]', "section"),
]


def _normalize_key(category: str, number: str | None) -> str:
    if number and number.isdigit():
        return f"{category}_{number}"
    return category


def find_si_references_in_text(
    doc: fitz.Document, page_texts: dict[int, str]
) -> list[SIReference]:
    """Find all SI references in the main article with their positions."""
    refs = []
    seen = set()

    for page_idx in sorted(page_texts.keys()):
        page = doc[page_idx]
        text = page_texts[page_idx]
        # Collapse whitespace/newlines for pattern matching but keep original for positioning
        text_flat = re.sub(r'\s+', ' ', text)

        for pattern, category in MAIN_TEXT_SI_PATTERNS:
            for m in re.finditer(pattern, text_flat):
                full_match = m.group(0)
                number = m.group(1) if m.lastindex and m.group(1).isdigit() else ""
                key = _normalize_key(category, number)

                dedup = (page_idx, key, full_match)
                if dedup in seen:
                    continue
                seen.add(dedup)

                # Try searching both original and whitespace-collapsed versions
                rects = page.search_for(full_match)
                if not rects:
                    compact = re.sub(r'\s+', ' ', full_match)
                    rects = page.search_for(compact)
                rect = rects[0] if rects else None

                refs.append(SIReference(
                    text=full_match,
                    page_idx=page_idx,
                    rect=rect,
                    target_key=key,
                ))
    return refs


def find_si_anchors(doc: fitz.Document) -> list[SIAnchor]:
    """Find headings/captions in the SI document."""
    anchors = []
    seen_keys = set()

    for page_idx in range(len(doc)):
        text = doc[page_idx].get_text()
        page = doc[page_idx]

        for pattern, category in SI_HEADING_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
                full_match = m.group(0).strip()
                number = m.group(1) if m.lastindex and m.group(1) else ""
                key = _normalize_key(category, number)

                if key in seen_keys:
                    continue
                seen_keys.add(key)

                # For section-header patterns like "\nS1 ", search for "S1" specifically
                search_text = full_match
                if category == "section" and re.match(r'\s*S\d+\s', full_match):
                    search_text = f"S{number}"

                rects = page.search_for(search_text)
                rect = rects[0] if rects else None

                anchors.append(SIAnchor(
                    text=search_text.strip(),
                    page_idx=page_idx,
                    rect=rect,
                    key=key,
                ))
    return anchors


# ---------------------------------------------------------------------------
# PDF Merge + Cross-Reference Links
# ---------------------------------------------------------------------------

LINK_COLOR = (0.0, 0.4, 0.8)  # blue


def merge_and_link(
    article_doc: fitz.Document,
    si_docs: list[tuple[fitz.Document, str]],  # (doc, label)
    references: list[SIReference],
    anchors: list[SIAnchor],
    output_path: str,
    on_progress: ProgressCallback = _noop_progress,
):
    """
    Merge article + SI PDFs and add cross-reference links.
    article_doc should always be the best-quality version (re-downloaded if needed).
    """
    merged = fitz.open()

    merged.insert_pdf(article_doc)
    article_page_count = len(article_doc)

    si_page_offsets = []
    for si_doc, label in si_docs:
        offset = len(merged)
        si_page_offsets.append(offset)
        merged.insert_pdf(si_doc)
        on_progress(6, "appending", f"Appended {label} ({len(si_doc)} pages)")

    # Build anchor lookup: key -> (absolute_page_idx, rect)
    anchor_map: dict[str, tuple[int, fitz.Rect | None]] = {}
    for anchor in anchors:
        abs_page = si_page_offsets[0] + anchor.page_idx if si_page_offsets else anchor.page_idx
        anchor_map[anchor.key] = (abs_page, anchor.rect)

    # Create cross-reference links
    link_count = 0
    for ref in references:
        if ref.target_key not in anchor_map:
            continue
        target_page, target_rect = anchor_map[ref.target_key]

        if ref.rect is None:
            continue

        source_page = merged[ref.page_idx]

        dest_point = fitz.Point(0, target_rect.y0 - 20) if target_rect else fitz.Point(0, 0)

        link = {
            "kind": fitz.LINK_GOTO,
            "from": ref.rect,
            "page": target_page,
            "to": dest_point,
        }
        source_page.insert_link(link)

        # Visual highlight on the reference text
        annot = source_page.add_underline_annot(ref.rect)
        annot.set_colors(stroke=LINK_COLOR)
        annot.update()

        link_count += 1

    # Add back-links from SI headings to the first reference in the article
    ref_map: dict[str, SIReference] = {}
    for ref in references:
        if ref.target_key not in ref_map:
            ref_map[ref.target_key] = ref

    back_link_count = 0
    for anchor in anchors:
        if anchor.key not in ref_map or anchor.rect is None:
            continue
        ref = ref_map[anchor.key]
        abs_page = si_page_offsets[0] + anchor.page_idx if si_page_offsets else anchor.page_idx
        si_page = merged[abs_page]

        dest_point = fitz.Point(0, ref.rect.y0 - 20) if ref.rect else fitz.Point(0, 0)
        link = {
            "kind": fitz.LINK_GOTO,
            "from": anchor.rect,
            "page": ref.page_idx,
            "to": dest_point,
        }
        si_page.insert_link(link)

        annot = si_page.add_underline_annot(anchor.rect)
        annot.set_colors(stroke=(0.8, 0.2, 0.0))  # orange for back-links
        annot.update()
        back_link_count += 1

    # Add outline (bookmarks) for navigation
    toc = []
    toc.append([1, "Original Article", 1])
    for i, (si_doc, label) in enumerate(si_docs):
        page_num = si_page_offsets[i] + 1  # 1-based for TOC
        toc.append([1, label, page_num])

    for anchor in anchors:
        abs_page = si_page_offsets[0] + anchor.page_idx + 1 if si_page_offsets else anchor.page_idx + 1
        toc.append([2, anchor.text, abs_page])

    merged.set_toc(toc)

    merged.save(output_path, garbage=4, deflate=True)
    on_progress(6, "done", f"Forward links: {link_count}, Back links: {back_link_count}")
    return merged


# ---------------------------------------------------------------------------
# Main Workflow (library API)
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    output_path: str
    doi: str
    title: str
    article_pages: int
    si_pages: int
    forward_links: int
    back_links: int
    si_files_found: list[dict]


def run_merge(
    pdf_path: str,
    output_path: str | None = None,
    doi_override: str | None = None,
    si_filter: str = "supplementary information",
    on_progress: ProgressCallback = _noop_progress,
    si_urls: list[str] | None = None,
    si_files_local: list[str] | None = None,
) -> MergeResult:
    """
    Core merge workflow. Usable as a library function (called by both CLI and web API).

    Args:
        si_urls: Optional list of direct SI download URLs. When provided, skips
                 automatic SI discovery (useful when auto-discovery fails).
        si_files_local: Optional list of local SI file paths (PDF/DOCX). When
                        provided, skips both discovery and download (useful for
                        publishers like APS that block all automated access).
    Raises exceptions on failure instead of calling sys.exit.
    """
    pdf_path = os.path.abspath(pdf_path)
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"File not found: {pdf_path}")

    work_dir = tempfile.mkdtemp(prefix="si_merge_")

    if output_path is None:
        base = Path(pdf_path).stem
        output_path = str(Path(pdf_path).parent / f"{base}_with_SI.pdf")

    # Reset HTTP session for fresh cookies per merge task
    global _session
    _session = None

    # Step 1: Extract DOI
    on_progress(1, "started", "Extracting DOI from PDF")
    original_doc = fitz.open(pdf_path)
    doi = doi_override or extract_doi(original_doc)
    title = (original_doc.metadata or {}).get("title", "Unknown")
    has_manual = bool(si_urls or si_files_local)
    if doi:
        on_progress(1, "done", f"DOI: {doi}")
    else:
        on_progress(1, "done", "DOI not found (continuing with manual SI)" if has_manual else "")
        if not has_manual:
            raise ValueError("Could not extract DOI from the PDF. Please provide it manually.")

    # Step 2 & 3: Find and download SI
    article_url = ""
    si_file_dicts: list[dict] = []

    if si_files_local:
        # Local files provided — skip discovery and download entirely
        on_progress(2, "started", "Using locally provided SI file(s)")
        downloaded = []
        for i, path in enumerate(si_files_local):
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"SI file not found: {path}")
            ext = Path(path).suffix.lower()
            if ext not in SUPPORTED_SI_EXTENSIONS:
                raise ValueError(f"Unsupported SI format: {ext}. Supported: {', '.join(SUPPORTED_SI_EXTENSIONS)}")
            label = Path(path).name
            # Convert non-PDF to PDF if needed
            if ext != ".pdf":
                on_progress(2, "converting", f"Converting {label} to PDF...")
                pdf_out = os.path.join(work_dir, f"si_{i+1}.pdf")
                if not _convert_to_pdf(path, pdf_out):
                    raise RuntimeError(f"Failed to convert {label} to PDF.")
                local_path = pdf_out
            else:
                local_path = path
            si = SIFile(url=f"file://{path}", label=label, local_path=local_path)
            downloaded.append(si)
            si_file_dicts.append({"label": label, "url": f"file://{path}"})
        on_progress(2, "done", f"Using {len(downloaded)} local SI file(s)")
        on_progress(3, "done", f"Skipped download ({len(downloaded)} local file(s))")

    elif si_urls:
        # URLs provided — skip discovery, but still download
        on_progress(2, "started", "Using manually provided SI URL(s)")
        filtered = [SIFile(url=u, label=Path(urllib.parse.urlparse(u).path).name or f"SI {i+1}")
                    for i, u in enumerate(si_urls)]
        si_file_dicts = [{"label": si.label, "url": si.url} for si in filtered]
        on_progress(2, "done", f"Using {len(filtered)} manually provided SI file(s)")

        on_progress(3, "started", "Downloading SI files")
        downloaded = download_si_files(filtered, work_dir, article_url=article_url, on_progress=on_progress)
        if not downloaded:
            raise FileNotFoundError(
                "Failed to download SI files. The publisher may block automated downloads. "
                "Try downloading the SI manually and use --si-file instead."
            )
        on_progress(3, "done", f"Downloaded {len(downloaded)} SI file(s)")

    else:
        # Auto-discovery
        on_progress(2, "started", "Resolving article URL")
        article_url = resolve_article_url(doi)
        if not article_url:
            raise ConnectionError(f"Could not resolve DOI {doi} to an article URL.")
        on_progress(2, "searching", f"Found article: {article_url}")

        si_files = find_si_links(article_url, on_progress)
        if not si_files:
            raise FileNotFoundError(
                "No supplementary information files found for this article. "
                "You can provide SI URLs with --si-url, or local files with --si-file."
            )

        si_file_dicts = [{"label": si.label, "url": si.url} for si in si_files]
        on_progress(2, "done", f"Found {len(si_files)} SI file(s)")

        has_supported_ext = lambda si: _get_file_ext(si.url) in SUPPORTED_SI_EXTENSIONS
        filtered = [si for si in si_files if has_supported_ext(si) and si_filter.lower() in si.label.lower()]
        if not filtered:
            filtered = [si for si in si_files if has_supported_ext(si)]
        if not filtered:
            raise FileNotFoundError(
                "No downloadable SI files found in supported formats (.pdf, .docx, .doc). "
                "You can provide SI URLs with --si-url, or local files with --si-file."
            )

        on_progress(3, "started", "Downloading SI files")
        downloaded = download_si_files(filtered, work_dir, article_url=article_url, on_progress=on_progress)
        if not downloaded:
            si_urls_hint = ", ".join(si.url for si in filtered[:3])
            raise FileNotFoundError(
                f"SI files were found but could not be downloaded (publisher blocked the request). "
                f"Use the Chrome extension for automatic download, or provide the SI manually. "
                f"SI URL(s): {si_urls_hint}"
            )
        on_progress(3, "done", f"Downloaded {len(downloaded)} SI file(s)")

    # Step 4: Extract text
    on_progress(4, "started", "Preparing text for cross-referencing")
    article_doc, page_texts = get_article_text(original_doc, doi, work_dir, on_progress)
    on_progress(4, "done", "Text extraction complete")

    # Step 5: Analyze references
    on_progress(5, "started", "Analyzing SI references")
    references = find_si_references_in_text(article_doc, page_texts)
    si_doc = fitz.open(downloaded[0].local_path)
    anchors = find_si_anchors(si_doc)
    on_progress(5, "done", f"Found {len(references)} references and {len(anchors)} anchors")

    # Step 6: Merge and link
    on_progress(6, "started", "Merging PDFs and creating cross-reference links")
    si_docs_with_labels = [(si_doc, downloaded[0].label)]
    for d in downloaded[1:]:
        si_docs_with_labels.append((fitz.open(d.local_path), d.label))

    merge_and_link(
        article_doc=article_doc,
        si_docs=si_docs_with_labels,
        references=references,
        anchors=anchors,
        output_path=output_path,
        on_progress=on_progress,
    )

    forward_links = sum(1 for r in references if r.rect and r.target_key in {a.key for a in anchors})
    back_links = sum(1 for a in anchors if a.rect and a.key in {r.target_key for r in references if r.rect})
    si_total_pages = sum(fitz.open(d.local_path).page_count for d in downloaded)

    on_progress(6, "complete", "Merge complete!")

    return MergeResult(
        output_path=output_path,
        doi=doi,
        title=title,
        article_pages=len(original_doc),
        si_pages=si_total_pages,
        forward_links=forward_links,
        back_links=back_links,
        si_files_found=si_file_dicts,
    )


# ---------------------------------------------------------------------------
# Batch Merge (library API)
# ---------------------------------------------------------------------------

@dataclass
class BatchMergeResult:
    total: int
    succeeded: int
    failed: int
    results: list[dict]   # each: {path, result: MergeResult|None, error: str|None}


BatchProgressCallback = Callable[[int, int, str, str], None]
"""(file_index, total_files, filename, status_message)"""


def run_batch_merge(
    pdf_paths: list[str],
    output_dir: str | None = None,
    si_filter: str = "supplementary information",
    on_batch_progress: BatchProgressCallback | None = None,
    on_file_progress: ProgressCallback = _noop_progress,
) -> BatchMergeResult:
    """
    Merge SI for multiple PDFs. Returns a summary of all results.

    Args:
        pdf_paths: List of PDF file paths to process.
        output_dir: Directory to save merged PDFs. Defaults to each PDF's own directory.
        si_filter: Keyword filter for SI files.
        on_batch_progress: Callback for overall batch progress (file_index, total, filename, status).
        on_file_progress: Callback for per-file step-level progress.
    """
    results = []
    succeeded = 0
    total = len(pdf_paths)

    for i, pdf_path in enumerate(pdf_paths):
        filename = Path(pdf_path).name
        if on_batch_progress:
            on_batch_progress(i, total, filename, "started")

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            out = str(Path(output_dir) / f"{Path(pdf_path).stem}_with_SI.pdf")
        else:
            out = None

        try:
            result = run_merge(pdf_path, out, None, si_filter, on_file_progress)
            results.append({"path": pdf_path, "result": result, "error": None})
            succeeded += 1
            if on_batch_progress:
                on_batch_progress(i, total, filename, "completed")
        except Exception as e:
            results.append({"path": pdf_path, "result": None, "error": str(e)})
            if on_batch_progress:
                on_batch_progress(i, total, filename, f"failed: {e}")

    return BatchMergeResult(
        total=total,
        succeeded=succeeded,
        failed=total - succeeded,
        results=results,
    )


# ---------------------------------------------------------------------------
# CLI Wrapper
# ---------------------------------------------------------------------------

def _cli_progress(step: int, status: str, detail: str = "") -> None:
    step_names = {1: "Extract DOI", 2: "Find SI", 3: "Download SI",
                  4: "Extract Text", 5: "Analyze References", 6: "Merge & Link"}
    name = step_names.get(step, f"Step {step}")
    print(f"  [{name}] {status}: {detail}")


def run(
    pdf_path: str,
    output_path: str | None = None,
    doi_override: str | None = None,
    si_filter: str = "supplementary information",
    si_urls: list[str] | None = None,
    si_files_local: list[str] | None = None,
):
    """CLI entry point for single file."""
    try:
        result = run_merge(pdf_path, output_path, doi_override, si_filter, _cli_progress,
                           si_urls=si_urls, si_files_local=si_files_local)
        print(f"\n{'=' * 60}")
        print(f"  Title:         {result.title}")
        print(f"  DOI:           {result.doi}")
        print(f"  Article pages: {result.article_pages}")
        print(f"  SI pages:      {result.si_pages}")
        print(f"  Forward links: {result.forward_links}")
        print(f"  Back links:    {result.back_links}")
        print(f"  Output:        {result.output_path}")
        print("=" * 60)
        return result.output_path
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


def run_batch_cli(pdf_paths: list[str], output_dir: str | None = None, si_filter: str = "supplementary information"):
    """CLI entry point for batch processing."""
    def on_batch(idx, total, filename, status):
        print(f"\n[{idx + 1}/{total}] {filename}: {status}")

    batch = run_batch_merge(pdf_paths, output_dir, si_filter, on_batch, _cli_progress)

    print(f"\n{'=' * 60}")
    print(f"  Batch complete: {batch.succeeded}/{batch.total} succeeded, {batch.failed} failed")
    print(f"{'=' * 60}")
    for entry in batch.results:
        name = Path(entry["path"]).name
        if entry["result"]:
            r = entry["result"]
            print(f"  OK   {name}")
            print(f"       -> {r.output_path}")
            print(f"       {r.article_pages} + {r.si_pages} pages, {r.forward_links} fwd / {r.back_links} back links")
        else:
            print(f"  FAIL {name}")
            print(f"       {entry['error']}")
    print(f"{'=' * 60}")
    return batch


def _collect_pdfs(paths: list[str]) -> list[str]:
    """Expand paths: files are kept as-is, directories are scanned for PDFs."""
    result = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.lower().endswith(".pdf") and not f.endswith("_with_SI.pdf"):
                    result.append(os.path.join(p, f))
        elif os.path.isfile(p):
            result.append(p)
        else:
            print(f"Warning: skipping '{p}' (not found)", file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Merge Supplementary Information into journal article PDFs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python si_merge.py article.pdf
  python si_merge.py article.pdf -o merged.pdf
  python si_merge.py article.pdf --doi 10.1038/s41467-023-44674-1
  python si_merge.py article.pdf --si-url https://example.com/si.pdf
  python si_merge.py article.pdf --si-file ~/Downloads/si.pdf   # local SI file
  python si_merge.py paper1.pdf paper2.pdf paper3.pdf            # batch mode
  python si_merge.py ./papers/ -o ./merged/                      # process entire folder
        """,
    )
    parser.add_argument("pdf", nargs="+", help="PDF file(s) or directory containing PDFs")
    parser.add_argument("-o", "--output", help="Output path: file (single) or directory (batch)")
    parser.add_argument("--doi", help="Manually specify DOI (single file mode only)")
    parser.add_argument(
        "--si-url", action="append", default=None, dest="si_urls",
        help="Direct SI download URL (repeatable; skips auto-discovery).",
    )
    parser.add_argument(
        "--si-file", action="append", default=None, dest="si_files",
        help="Local SI file path (repeatable; skips discovery and download). "
             "Use when the publisher blocks all automated access (e.g. APS).",
    )
    parser.add_argument(
        "--si-filter",
        default="supplementary information",
        help="Filter SI files by label keyword (default: 'supplementary information')",
    )
    args = parser.parse_args()

    pdf_paths = _collect_pdfs(args.pdf)
    if not pdf_paths:
        print("Error: no PDF files found.", file=sys.stderr)
        sys.exit(1)

    if len(pdf_paths) == 1 and not os.path.isdir(args.pdf[0]):
        run(pdf_paths[0], args.output, args.doi, args.si_filter,
            si_urls=args.si_urls, si_files_local=args.si_files)
    else:
        if args.doi:
            print("Warning: --doi is ignored in batch mode.", file=sys.stderr)
        if args.si_urls or args.si_files:
            print("Warning: --si-url/--si-file are ignored in batch mode.", file=sys.stderr)
        run_batch_cli(pdf_paths, args.output, args.si_filter)


if __name__ == "__main__":
    main()
